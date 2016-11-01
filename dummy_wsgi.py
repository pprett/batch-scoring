import flask

app = flask.Flask(__name__)

print('barfoo')

@app.route('/api/v1/<pid>/<model_id>/predict', methods=['POST'])
def predict(*args, **kw):
    try:
        print('foobar')
        content = flask.request.get_data()
        n_rows = len(content.split('\n'))

        resp = {'execution_time': 0.0,
                'predictions': [{'prediction': float(i), 'row_id': i}
                                for i, _ in enumerate(xrange(n_rows))],
                'task': 'Regression',
                }

        return flask.jsonify(**resp)
    except Exception as e:
        print(e)
        raise


@app.route('/ping', methods=['GET'])
def ping():
    print('ping')
    return flask.jsonify(foo='bar')
