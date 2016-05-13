# -*- coding: utf-8 -*-
from __future__ import print_function

import codecs
import collections
import csv
import glob
import gzip
import io
import json
import operator
import os
import shelve
import sys
import threading
import hashlib
import queue
from functools import partial, reduce
from itertools import chain, islice
from pprint import pformat
from time import time
from time import sleep
from multiprocessing import Queue
from multiprocessing import Process

import requests
import six

from .network import Network
from .utils import acquire_api_token, iter_chunks


if six.PY2:  # pragma: no cover
    from contextlib2 import ExitStack
    import dumbdbm  # noqa
elif six.PY3:  # pragma: no cover
    from contextlib import ExitStack
    # for successful py2exe dist package
    from dbm import dumb  # noqa


class ShelveError(Exception):
    pass


Batch = collections.namedtuple('Batch', 'id fieldnames data rty_cnt')
Prediction = collections.namedtuple('Prediction', 'fieldnames data')

SENTINEL = Batch(-1, None, '', -1)


class TargetType(object):
    REGRESSION = 'Regression'
    BINARY = 'Binary'

def lines_to_csv_chunk(data, header):
    return ''.join(chain([header,], data))

def to_csv_chunk(data, fieldnames):
    # otherwise we make an async request
    if six.PY3:
        out = io.StringIO()
    else:
        out = io.BytesIO()
    writer = csv.writer(out)
    writer.writerow(fieldnames)
    writer.writerows(data)
    data = out.getvalue().encode('latin-1')
    return data


class BatchGenerator(object):
    """Class to chunk a large csv files into a stream
    of batches of size ``--n_samples``.

    Yields
    ------
    batch : Batch
        The next batch
    """

    def __init__(self, dataset, n_samples, n_retry, delimiter, ui):
        self.dataset = dataset
        self.chunksize = n_samples
        self.rty_cnt = n_retry
        self.sep = delimiter
        self._ui = ui

    def open(self):
        if self.dataset.endswith('.gz'):
            return gzip.open(self.dataset)
        else:
            if six.PY2:
                return open(self.dataset, 'rb')
            else:
                return open(self.dataset, 'rb')

    def __iter__(self):
        rows_read = 0
        sep = self.sep

        # handle unix tabs
        with self.open() as csvfile:
            sniffer = csv.Sniffer()
            dialect = sniffer.sniff(csvfile.read(1024).decode('latin-1'))

        if sep is not None:
            # if fixed sep check if we have at least one occurrence.
            with self.open() as fd:
                header = fd.readline()
                if isinstance(header, bytes):
                    bsep = sep.encode('utf-8')
                else:
                    bsep = sep
                if not header.strip():
                    raise ValueError("Input file '{}' is empty."
                                     .format(self.dataset))
                if len(header.split(bsep)) == 1:
                    raise ValueError(
                        ("Delimiter '{}' not found. "
                         "Please check your input file "
                         "or consider the flag `--delimiter=''`.").format(sep))
        if sep is None:
            sep = dialect.delimiter

        csvfile = codecs.getreader('latin-1')(self.open())
        #reader = csv.reader(csvfile, dialect, delimiter=sep)
        reader = iter(csvfile)
        header = next(reader)
        fieldnames = [c.strip() for c in header.split(sep)]

        batch_num = None
        _chunk = None
        for batch_num, chunk in enumerate(c for c in islice(iter_chunks(reader,
                                                                      self.chunksize), 100) for i in range(4)):
            if batch_num == 0:
                self._ui.debug('input head: {}'.format(pformat(chunk[:50])))

            rows_read += len(chunk)
            if _chunk is None:
                chunk = lines_to_csv_chunk(chunk, header)
            else:
                chunk = _chunk
            yield Batch(rows_read, fieldnames, chunk, self.rty_cnt)
        if batch_num is None:
            raise ValueError("Input file '{}' is empty.".format(self.dataset))


def peek_row(dataset, delimiter, ui):
    """Peeks at the first row in `dataset`. """
    batches = BatchGenerator(dataset, 1, 1, delimiter, ui)
    try:
        batch = next(iter(batches))
    except StopIteration:
        raise ValueError('Cannot peek first row from {}'.format(dataset))
    return batch


class GeneratorBackedQueue(object):
    """A queue that is backed by a generator.

    When the queue is exhausted it repopulates from the generator.
    """

    def __init__(self, gen):
        self.gen = gen
        self.n_consumed = 0
        self.deque = collections.deque()
        self.lock = threading.RLock()

    def __iter__(self):
        return self

    def __next__(self):
        with self.lock:
            if len(self.deque):
                return self.deque.popleft()
            else:
                out = next(self.gen)
                self.n_consumed += 1
                return out

    def next(self):
        return self.__next__()

    def push(self, batch):
        # we retry a batch - decrement retry counter
        with self.lock:
            batch = batch._replace(rty_cnt=batch.rty_cnt - 1)
            self.deque.append(batch)

    def has_next(self):
        with self.lock:
            try:
                item = self.next()
                self.push(item)
                return True
            except StopIteration:
                return False


class MultiprocessingGeneratorBackedQueue(object):
    """A queue that is backed by a generator.

    When the queue is exhausted it repopulates from the generator.
    """
    def __init__(self, queue_size, ui):
        print('queue size: {}'.format(queue_size))
        self.n_consumed = 0
        self.queue = Queue(queue_size)
        self.deque = Queue(queue_size)
        self.lock = threading.RLock()
        self._ui = ui

    def __iter__(self):
        return self

    def __next__(self):
        try:
            r = self.deque.get_nowait()
            return r
            print('PETER GOT SOMETHING')
        except queue.Empty:
            try:
                r = self.queue.get()
                if r.id == SENTINEL.id:
                    print('BITTER PILL')
                    self.queue.close()
                    raise StopIteration
                self.n_consumed += 1
                return r
            except OSError:
                raise StopIteration

    def __len__(self):
        return self.queue.qsize() + self.deque.qsize()

    def next(self):
        return self.__next__()

    def push(self, batch):
        # we retry a batch - decrement retry counter
        batch = batch._replace(rty_cnt=batch.rty_cnt - 1)
        try:
            self.deque.put(batch, block=False)
        except queue.Empty:
            self._ui.error('Dropping {} due to backfill queue full.'.format(batch))

    def has_next(self):
        with self.lock:
            try:
                item = self.next()
                self.push(item)
                return True
            except StopIteration:
                return False


class Shovel(object):

    def __init__(self, ctx, queue, ui):
        self.ctx = ctx
        self.queue = queue
        self._ui = ui

    def _shove(self, q, ctx):
        for batch in ctx.batch_generator():
            q.put(batch)

        q.put(SENTINEL)

    def go(self):
        self.p = Process(target=self._shove, args=(self.queue.queue, self.ctx))
        print('#### Shoving hard...')
        self.p.start()

    def all(self):
        self.queue.queue = Queue(0)
        self._shove(self.queue.queue, self.ctx)


def process_successful_request(result, batch, ctx, pred_name):
    """Process a successful request. """
    predictions = result['predictions']
    if result['task'] == TargetType.BINARY:
        sorted_classes = list(
            sorted(predictions[0]['class_probabilities'].keys()))
        out_fields = ['row_id'] + sorted_classes
        if pred_name is not None and '1.0' in sorted_classes:
            sorted_classes = ['1.0']
            out_fields = ['row_id'] + [pred_name]
        pred = [[p['row_id'] + batch.id] +
                [p['class_probabilities'][c] for c in sorted_classes]
                for p in
                sorted(predictions, key=operator.itemgetter('row_id'))]
    elif result['task'] == TargetType.REGRESSION:
        pred = [[p['row_id'] + batch.id, p['prediction']]
                for p in
                sorted(predictions, key=operator.itemgetter('row_id'))]
        out_fields = ['row_id', pred_name if pred_name else '']
    else:
        ValueError('task {} not supported'.format(result['task']))

    ctx.checkpoint_batch(batch, out_fields, pred)


class WorkUnitGenerator(object):
    """Generates async requests with completion or retry callbacks.

    It uses a queue backed by a batch generator.
    It will pop items for the queue and if its exhausted it will populate the
    queue from the batch generator.
    If a submitted async request was not successfull it gets enqueued again.
    """

    def __init__(self, queue, endpoint, headers, user, api_token,
                 ctx, pred_name, ui):
        self.endpoint = endpoint
        self.headers = headers
        self.user = user
        self.api_token = api_token
        self.ctx = ctx
        self.queue = queue
        self.pred_name = pred_name
        self._ui = ui

    def _response_callback(self, r, batch=None, *args, **kw):
        try:
            if r.status_code == 200:
                try:
                    try:
                        result = r.json()
                    except Exception as e:
                        self._ui.warning('{} response error: {} -- retry'
                                         .format(batch.id, e))
                        self.queue.push(batch)
                        return
                    exec_time = result['execution_time']
                    self._ui.debug(('successful response: exec time '
                                    '{:.0f}msec |'
                                    ' round-trip: {:.0f}msec').format(
                                        exec_time,
                                        r.elapsed.total_seconds() * 1000))

                    process_successful_request(result, batch,
                                               self.ctx, self.pred_name)
                except Exception as e:
                    self._ui.fatal('{} response error: {}'.format(batch.id, e))
            else:
                try:
                    self._ui.warning('batch {} failed with status: {}'
                                     .format(batch.id,
                                             json.loads(r.text)['status']))
                except ValueError:
                    self._ui.warning('batch {} failed with status code: {}'
                                     .format(batch.id, r.status_code))

                text = r.text
                self._ui.error('batch {} failed status_code:{} text:{}'
                               .format(batch.id,
                                       r.status_code,
                                       text))
                self.queue.push(batch)
        except Exception as e:
            self._ui.error('batch {} - dropping due to: {}'
                           .format(batch.id, e))

    def has_next(self):
        return self.queue.has_next()

    def __iter__(self):
        for i, batch in enumerate(self.queue):
            if batch.id == -1:  # sentinel
                print('Got sentinel')
                raise StopIteration()
            # if we exhaused our retries we drop the batch
            if batch.rty_cnt == 0:
                self._ui.error('batch {} exceeded retry limit; '
                               'we lost {} records'.format(
                                   batch.id, len(batch.data)))
                continue
            self._ui.debug('batch {} transmitting {} bytes'
                           .format(batch.id, len(batch.data)))
            hook = partial(self._response_callback, batch=batch)
            yield requests.Request(
                method='POST',
                url=self.endpoint,
                headers=self.headers,
                data=batch.data,
                auth=(self.user, self.api_token),
                hooks={'response': hook})
            if i % 20 == 0:
                self._ui.info('{} still in queue'.format(len(self.queue)))


class RunContext(object):
    """A context for a run backed by a persistant store.

    We use a shelve to store the state of the run including
    a journal of processed batches that have been checkpointed.

    Note: we use globs for the shelve files because different
    versions of Python have different file layouts.
    """

    def __init__(self, n_samples, out_file, pid, lid, keep_cols,
                 n_retry, delimiter, dataset, pred_name, ui, file_context):
        self.n_samples = n_samples
        self.out_file = out_file
        self.project_id = pid
        self.model_id = lid
        self.keep_cols = keep_cols
        self.n_retry = n_retry
        self.delimiter = delimiter
        self.dataset = dataset
        self.pred_name = pred_name
        self.out_stream = None
        self.lock = threading.Lock()
        self._ui = ui
        self.file_context = file_context

    @classmethod
    def create(cls, resume, n_samples, out_file, pid, lid,
               keep_cols, n_retry,
               delimiter, dataset, pred_name, ui):
        """Factory method for run contexts.

        Either resume or start a new one.
        """
        file_context = ContextFile(pid, lid, n_samples, keep_cols)
        if file_context.exists():
            is_resume = None
            if resume:
                is_resume = True
            if is_resume is None:
                is_resume = ui.prompt_yesno('Existing run found. Resume')
        else:
            is_resume = False
        if is_resume:
            ctx_class = OldRunContext
        else:
            ctx_class = NewRunContext

        return ctx_class(n_samples, out_file, pid, lid, keep_cols, n_retry,
                         delimiter, dataset, pred_name, ui, file_context)

    def __enter__(self):
        self.db = shelve.open(self.file_context.file_name, writeback=True)
        self.partitions = []
        return self

    def __exit__(self, type, value, traceback):
        self.db.close()
        if self.out_stream is not None:
            self.out_stream.close()
        if type is None:
            # success - remove shelve
            self.file_context.clean()

    def checkpoint_batch(self, batch, out_fields, pred):
        if self.keep_cols:
            # stack columns
            if self.db['first_write']:
                if not all(c in batch.fieldnames for c in self.keep_cols):
                    self._ui.fatal('keep_cols "{}" not in columns {}.'.format(
                        [c for c in self.keep_cols
                         if c not in batch.fieldnames],
                        batch.fieldnames))

            indices = [i for i, col in enumerate(batch.fieldnames)
                       if col in self.keep_cols]
            # first column is row_id
            comb = []
            written_fields = ['row_id'] + self.keep_cols + out_fields[1:]
            for origin, predicted in zip(batch.data, pred):
                keeps = [origin[i] for i in indices]
                comb.append([predicted[0]] + keeps + predicted[1:])
        else:
            comb = pred
            written_fields = out_fields
        with self.lock:
            # if an error happends during/after the append we
            # might end up with inconsistent state
            # TODO write partition files instead of appending
            #  store checksum of each partition and back-check
            writer = csv.writer(self.out_stream)
            if self.db['first_write']:
                writer.writerow(written_fields)
            writer.writerows(comb)
            self.out_stream.flush()

            self.db['checkpoints'].append(batch.id)

            self.db['first_write'] = False
            self._ui.info('batch {} checkpointed'.format(batch.id))
            self.db.sync()

    def batch_generator(self):
        return iter(BatchGenerator(self.dataset, self.n_samples,
                                   self.n_retry, self.delimiter, self._ui))


class ContextFile(object):
    def __init__(self, project_id, model_id, n_samples, keep_cols):
        hashable = reduce(operator.add, map(str,
                                            [project_id,
                                             model_id,
                                             n_samples,
                                             keep_cols]))
        digest = hashlib.md5(hashable.encode('utf8')).hexdigest()
        self.file_name = digest + '.shelve'

    def exists(self):
        """Does shelve exist. """
        return any(glob.glob(self.file_name + '*'))

    def clean(self):
        """Clean the shelve. """
        for fname in glob.glob(self.file_name + '*'):
            os.remove(fname)


class NewRunContext(RunContext):
    """RunContext for a new run.

    It creates a shelve file and adds a checkpoint journal.
    """

    def __enter__(self):
        if self.file_context.exists():
            self._ui.info('Removing old run shelve')
            self.file_context.clean()
        if os.path.exists(self.out_file):
            self._ui.warning('File {} exists.'.format(self.out_file))
            rm = self._ui.prompt_yesno('Do you want to remove {}'.format(
                self.out_file))
            if rm:
                os.remove(self.out_file)
            else:
                sys.exit(0)

        super(NewRunContext, self).__enter__()

        self.db['n_samples'] = self.n_samples
        self.db['project_id'] = self.project_id
        self.db['model_id'] = self.model_id
        self.db['keep_cols'] = self.keep_cols
        # list of batch ids that have been processed
        self.db['checkpoints'] = []
        # used to check if output file is dirty (ie first write op)
        self.db['first_write'] = True
        self.db.sync()

        self.out_stream = open(self.out_file, 'w+')
        return self

    def __exit__(self, type, value, traceback):
        super(NewRunContext, self).__exit__(type, value, traceback)


class OldRunContext(RunContext):
    """RunContext for a resume run.

    It requires a shelve file and plays back the checkpoint journal.
    Checks if inputs are consistent.

    TODO: add md5sum of dataset otherwise they might
    use a different file for resume.
    """

    def __enter__(self):
        if not self.file_context.exists():
            raise ValueError('Cannot resume a project without {}'
                             .format(self.FILENAME))
        super(OldRunContext, self).__enter__()

        if self.db['n_samples'] != self.n_samples:
            raise ShelveError('n_samples mismatch: should be {} but was {}'
                              .format(self.db['n_samples'], self.n_samples))
        if self.db['project_id'] != self.project_id:
            raise ShelveError('project id mismatch: should be {} but was {}'
                              .format(self.db['project_id'], self.project_id))
        if self.db['model_id'] != self.model_id:
            raise ShelveError('model id mismatch: should be {} but was {}'
                              .format(self.db['model_id'], self.model_id))
        if self.db['keep_cols'] != self.keep_cols:
            raise ShelveError('keep_cols mismatch: should be {} but was {}'
                              .format(self.db['keep_cols'], self.keep_cols))

        self.out_stream = open(self.out_file, 'a')

        self._ui.info('resuming a shelved run with {} checkpointed batches'
                      .format(len(self.db['checkpoints'])))
        return self

    def __exit__(self, type, value, traceback):
        super(OldRunContext, self).__exit__(type, value, traceback)

    def batch_generator(self):
        """We filter everything that has not been checkpointed yet. """
        self._ui.info('playing checkpoint log forward.')
        already_processed_batches = set(self.db['checkpoints'])
        return (b for b in BatchGenerator(self.dataset,
                                          self.n_samples,
                                          self.n_retry,
                                          self.delimiter,
                                          self._ui)
                if b.id not in already_processed_batches)


def authorize(user, api_token, n_retry, endpoint, base_headers, batch, ui):
    """Check if user is authorized for the given model and that schema is correct.

    This function will make a sync request to the api endpoint with a single
    row just to make sure that the schema is correct and the user
    is authorized.
    """
    r = None

    while n_retry:
        ui.debug('request authorization')
        try:
            r = requests.post(endpoint, headers=base_headers,
                              data=batch.data,
                              auth=(user, api_token))
            ui.debug('authorization request response: {}|{}'
                     .format(r.status_code, r.text))
            if r.status_code == 200:
                # all good
                break
            if r.status_code == 400:
                # client error -- maybe schema is wrong
                try:
                    msg = r.json()['status']
                except:
                    msg = r.text

                ui.fatal('failed with client error: {}'.format(msg))
            elif r.status_code == 401:
                ui.fatal('failed to authenticate -- '
                         'please check your username and/or api token.')
            elif r.status_code == 405:
                ui.fatal('failed to request endpoint -- '
                         'please check your --host argument.')
        except requests.exceptions.ConnectionError:
            ui.error('cannot connect to {}'.format(endpoint))
        n_retry -= 1

    if n_retry == 0:
        status = r.text if r is not None else 'UNKNOWN'
        try:
            status = r.json()['status']
        except:
            pass  # fall back to r.text
        content = r.content if r is not None else 'NO CONTENT'
        ui.debug("Failed authorization response \n{!r}".format(content))
        ui.fatal(('authorization failed -- '
                  'please check project id and model id permissions: {}')
                 .format(status))
    else:
        ui.debug('authorization has succeeded')


def run_batch_predictions(base_url, base_headers, user, pwd,
                          api_token, create_api_token,
                          pid, lid, n_retry, concurrent,
                          resume, n_samples,
                          out_file, keep_cols, delimiter,
                          dataset, pred_name,
                          timeout, ui):
    t1 = time()
    if not api_token:
        if not pwd:
            pwd = ui.getpass()
        try:
            api_token = acquire_api_token(base_url, base_headers, user, pwd,
                                          create_api_token, ui)
        except Exception as e:
            ui.fatal(str(e))

    base_headers['content-type'] = 'text/csv; charset=utf8'
    endpoint = base_url + '/'.join((pid, lid, 'predict'))

    # Make a sync request to check authentication and fail early
    first_row = peek_row(dataset, delimiter, ui)
    ui.debug('First row for auth request: {}'.format(first_row))
    authorize(user, api_token, n_retry, endpoint, base_headers, first_row, ui)

    with ExitStack() as stack:
        ctx = stack.enter_context(
            RunContext.create(resume, n_samples, out_file, pid,
                              lid, keep_cols, n_retry, delimiter,
                              dataset, pred_name, ui))
        network = stack.enter_context(Network(concurrent, timeout))
        n_batches_checkpointed_init = len(ctx.db['checkpoints'])
        ui.debug('number of batches checkpointed initially: {}'
                 .format(n_batches_checkpointed_init))

        # make the queue twice as big as the
        queue = MultiprocessingGeneratorBackedQueue(concurrent * 2, ui)
        shovel = Shovel(ctx, queue, ui)
        print('## GOGOGO')
        #shovel.go()
        shovel.all()
        ui.info('shoveling complete | total time elapsed {}s'
                    .format(time() - t1))
        work_unit_gen = WorkUnitGenerator(queue,
                                          endpoint,
                                          headers=base_headers,
                                          user=user,
                                          api_token=api_token,
                                          ctx=ctx,
                                          pred_name=pred_name,
                                          ui=ui)
        t0 = time()
        i = 0

        responses = network.perform_requests(
            work_unit_gen)
        ui.info('done done done')
        for r in responses:
            i += 1
            ui.info('{} responses sent | time elapsed {}s'
                    .format(i, time() - t0))

        ui.debug('{} items still in the queue'
                 .format(len(work_unit_gen.queue)))

        print('before wait')
        #shovel.wait_for_bottom()
        ui.debug('list of checkpointed batches: {}'
                 .format(sorted(ctx.db['checkpoints'])))
        n_batches_checkpointed = (len(ctx.db['checkpoints']) -
                                  n_batches_checkpointed_init)
        ui.debug('number of batches checkpointed: {}'
                 .format(n_batches_checkpointed))
        n_batches_not_checkpointed = (work_unit_gen.queue.n_consumed -
                                      n_batches_checkpointed)
        batches_missing = n_batches_not_checkpointed > 0
        if batches_missing:
            ui.fatal(('scoring incomplete, {} batches were dropped | '
                      'time elapsed {}s')
                     .format(n_batches_not_checkpointed, time() - t0))
        else:
            ui.info('scoring complete | time elapsed {}s'
                    .format(time() - t0))
            ui.info('scoring complete | total time elapsed {}s'
                    .format(time() - t1))
            ui.close()
