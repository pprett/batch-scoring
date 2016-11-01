import flask

app = flask.Flask(__name__)


@app.route('/v1/<pid>/<model_id>/predict', methods=['POST'])
def predict(*args, **kw):
    content = flask.request.get_data()
    n_rows = len(content.split('\n')) - 2
    print(n_rows)

    resp = {'execution_time': 0.0,
            'predictions': [{'prediction': '1.0', 'row_id': i,
                             'class_probabilities': {'1.0': 0.5, '0.0': 0.5}}
                            for i, _ in enumerate(xrange(n_rows))],
            'task': 'Regression',
            }

    return flask.jsonify(**resp)


@app.route('/v1/ping', methods=['GET'])
def ping():
    print('ping')
    return flask.jsonify(foo='bar')
