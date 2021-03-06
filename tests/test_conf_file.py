import os
import mock
from tempfile import NamedTemporaryFile
import textwrap
from datarobot_batch_scoring.utils import (get_config_file,
                                           CONFIG_FILENAME,
                                           parse_config_file)
from datarobot_batch_scoring.main import main as batch_scoring_main


def test_no_file_is_ok():
    assert get_config_file() is None


def test_file_from_home_directory():
    with open(os.path.join(
            os.path.expanduser('~'),
            CONFIG_FILENAME), 'w'):
        try:
            assert get_config_file() == os.path.join(
                os.path.expanduser('~'),
                CONFIG_FILENAME)
        finally:
            os.remove(os.path.expanduser('~') + '/' + CONFIG_FILENAME)


def test_file_from_working_directory():
    with open(os.path.join(os.getcwd(),
                           CONFIG_FILENAME), 'w'):
        try:
            assert get_config_file() == os.path.join(os.getcwd(),
                                                     CONFIG_FILENAME)
        finally:
            os.remove(os.path.join(os.getcwd(),
                                   CONFIG_FILENAME))


def test_empty_file_doesnt_error():
    with NamedTemporaryFile(suffix='*.ini') as test_file:
        assert parse_config_file(test_file.name) == {}


def test_section_basic_with_username():
    raw_data = textwrap.dedent("""\
        [batch_scoring]
        host=file_host
        project_id=file_project_id
        model_id=file_model_id
        user=file_username
        password=file_password""")
    with NamedTemporaryFile(suffix='*.ini') as test_file:
        test_file.write(str(raw_data).encode('utf-8'))
        test_file.seek(0)
        parsed_result = parse_config_file(test_file.name)
        assert isinstance(parsed_result, dict)
        assert parsed_result['host'] == 'file_host'
        assert parsed_result['project_id'] == 'file_project_id'
        assert parsed_result['model_id'] == 'file_model_id'
        assert parsed_result['user'] == 'file_username'
        assert parsed_result['password'] == 'file_password'


def test_run_main_with_conf_file(monkeypatch):
    main_args = ['--host',
                 'http://localhost:53646/api',
                 '56dd9570018e213242dfa93c',
                 '56dd9570018e213242dfa93d',
                 'tests/fixtures/temperatura_predict.csv',
                 '--n_samples',
                 '10',
                 '--n_concurrent', '1', '--no']
    raw_data = textwrap.dedent("""\
        [batch_scoring]
        host=file_host
        project_id=file_project_id
        model_id=file_model_id
        user=file_username
        password=file_password""")
    with NamedTemporaryFile(suffix='*.ini') as test_file:
        test_file.write(str(raw_data).encode('utf-8'))
        test_file.seek(0)
        monkeypatch.setattr(
            'datarobot_batch_scoring.main.get_config_file',
            lambda: test_file.name)
        with mock.patch(
                'datarobot_batch_scoring.main'
                '.run_batch_predictions') as mock_method:
            batch_scoring_main(argv=main_args)
            mock_method.assert_called_once_with(
                base_url='http://localhost:53646/api/v1/',
                base_headers={},
                user='file_username',
                pwd='file_password',
                api_token=None,
                create_api_token=False,
                pid='56dd9570018e213242dfa93c',
                lid='56dd9570018e213242dfa93d',
                n_retry=3,
                concurrent=1,
                resume=False,
                n_samples=10,
                out_file='out.csv',
                keep_cols=None,
                delimiter=None,
                dataset='tests/fixtures/temperatura_predict.csv',
                pred_name=None,
                timeout=30,
                ui=mock.ANY
            )


def test_run_empty_main_with_conf_file(monkeypatch):
    main_args = []
    raw_data = textwrap.dedent("""\
        [batch_scoring]
        host=http://localhost:53646/api
        project_id=56dd9570018e213242dfa93c
        model_id=56dd9570018e213242dfa93d
        user=file_username
        password=file_password
        n_concurrent=1
        n_retry=3
        n_samples=10
        dataset=tests/fixtures/temperatura_predict.csv""")
    with NamedTemporaryFile(suffix='*.ini') as test_file:
        test_file.write(str(raw_data).encode('utf-8'))
        test_file.seek(0)
        monkeypatch.setattr(
            'datarobot_batch_scoring.main.get_config_file',
            lambda: test_file.name)
        with mock.patch(
                'datarobot_batch_scoring.main'
                '.run_batch_predictions') as mock_method:
            batch_scoring_main(argv=main_args)
            mock_method.assert_called_once_with(
                base_url='http://localhost:53646/api/v1/',
                base_headers={},
                user='file_username',
                pwd='file_password',
                api_token=None,
                create_api_token=False,
                pid='56dd9570018e213242dfa93c',
                lid='56dd9570018e213242dfa93d',
                n_retry=3,
                concurrent=1,
                resume=False,
                n_samples=10,
                out_file='out.csv',
                keep_cols=None,
                delimiter=None,
                dataset='tests/fixtures/temperatura_predict.csv',
                pred_name=None,
                timeout=30,
                ui=mock.ANY
            )
