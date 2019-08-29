import json
import argparse
import boto3
import logging
from botocore.exceptions import ClientError
from flask import abort, Flask, request, Response, render_template

from indra.util.aws import get_s3_file_tree
from indra.statements import get_all_descendants, IncreaseAmount, \
    DecreaseAmount, Activation, Inhibition, AddModification, \
    RemoveModification, get_statement_by_name

from emmaa.model import load_config_from_s3
from emmaa.answer_queries import QueryManager, load_model_manager_from_s3
from emmaa.queries import PathProperty, get_agent_from_text, GroundingError


app = Flask(__name__)
app.config['DEBUG'] = True
logger = logging.getLogger(__name__)


TITLE = 'emmaa title'
link_list = [('./home', 'EMMAA Dashboard'),
             ('./query', 'Queries')]


qm = QueryManager()


def _get_models():
    s3 = boto3.client('s3')
    resp = s3.list_objects(Bucket='emmaa', Prefix='models/', Delimiter='/')
    model_data = []
    for pref in resp['CommonPrefixes']:
        model = pref['Prefix'].split('/')[1]
        config_json = get_model_config(model)
        if not config_json:
            continue
        model_data.append((model, config_json))
    return model_data


def get_model_config(model):
    if model in model_cache:
        return model_cache[model]
    try:
        config_json = load_config_from_s3(model)
        model_cache[model] = config_json
    except ClientError:
        logger.warning(f"Model {model} has no metadata. Skipping...")
        return None
    if 'human_readable_name' not in config_json.keys():
        logger.warning(f"Model {model} has no readable name. Skipping...")
        model_cache[model] = None
    return model_cache[model]


def get_model_stats(model):
    s3 = boto3.client('s3')

    # Need jsons for model meta data and test statistics. File name examples:
    # stats/skcm/stats_2019-08-20-17-34-40.json
    overlap = 'stats_'
    prefix = f'stats/{model}/stats_'
    model_stats_files = _file_tree_list(prefix=prefix)
    try:
        latest_file = model_stats_files.pop()
        while latest_file and not latest_file.endswith('.json'):
            latest_file = model_stats_files.pop()
    except IndexError:
        logger.warning(f'Could not get data for model "{model}"')
        return json.dumps('')

    latest_file_key = 'stats_'.join([prefix.split(overlap)[0],
                                     latest_file.split(overlap)[1]])
    model_data_object = s3.get_object(Bucket='emmaa', Key=latest_file_key)
    return json.loads(model_data_object['Body'].read().decode('utf8'))


def model_last_updated(model):
    """Find the most recent pickle file of model and return its creation date

    Example file name:
    models/aml/model_2018-12-13-18-11-54.pkl

    Parameters
    ----------
    model : str
        model name

    Returns
    -------
    last_updated : str
        A string of the format "YYYY-MM-DD-HH-mm-ss"
    """
    prefix = f'models/{model}/model_'
    latest_model_files = _file_tree_list(prefix=prefix)
    try:
        latest_file = latest_model_files.pop()
        while latest_file and not latest_file.endswith('.pkl'):
            latest_file = latest_model_files.pop()
        return latest_file.split('.')[0].split('_')[1]
    except IndexError:
        logger.warning(f'No pickle files exist for model {model}')
        return


def _file_tree_list(prefix):
    s3 = boto3.client('s3')
    return sorted(get_s3_file_tree(s3=s3, bucket='emmaa', prefix=prefix))


GLOBAL_PRELOAD = False
model_cache = {}
if GLOBAL_PRELOAD:
    # Load all the model configs
    models = _get_models()
    # Load all the model managers for queries
    for model, _ in models:
        load_model_manager_from_s3(model)


def get_queryable_stmt_types():
    """Return Statement class names that can be used for querying."""
    def get_sorted_descendants(cls):
        return sorted(get_names(get_all_descendants(cls)))

    def get_names(classes):
        return [s.__name__ for s in classes]

    stmt_types = \
        get_names([Activation, Inhibition, IncreaseAmount, DecreaseAmount]) + \
        get_sorted_descendants(AddModification) + \
        get_sorted_descendants(RemoveModification)
    return stmt_types


def _make_query(query_dict, use_grouding_service=True):
    stmt_type = query_dict['typeSelection']
    stmt_class = get_statement_by_name(stmt_type)
    subj = get_agent_from_text(
        query_dict['subjectSelection'], use_grouding_service)
    obj = get_agent_from_text(
        query_dict['objectSelection'], use_grouding_service)
    stmt = stmt_class(subj, obj)
    query = PathProperty(path_stmt=stmt)
    return query


@app.route('/')
@app.route('/home')
def get_home():
    model_data = _get_models()
    return render_template('index_template.html',
                           model_data=model_data,
                           link_list=link_list)


@app.route('/dashboard/<model>')
def get_model_dashboard(model):
    model_data = _get_models()
    mod_link_list = [('.' + t[0], t[1]) for t in link_list]
    return render_template('model_template.html',
                           model=model,
                           model_data=model_data,
                           link_list=mod_link_list)


@app.route('/query')
def get_query_page():
    # TODO Should pass user specific info in the future when logged in
    model_data = _get_models()
    stmt_types = get_queryable_stmt_types()

    user_email = 'joshua@emmaa.com'
    old_results = qm.get_registered_queries(user_email)

    return render_template('query_template.html', model_data=model_data,
                           stmt_types=stmt_types, old_results=old_results,
                           link_list=link_list)


@app.route('/query/submit', methods=['POST'])
def process_query():
    # Print inputs.
    logger.info('Got model query')
    logger.info("Args -----------")
    logger.info(request.args)
    logger.info("Json -----------")
    logger.info(str(request.json))
    logger.info("------------------")

    # Extract info.
    expected_query_keys = {f'{pos}Selection'
                           for pos in ['subject', 'object', 'type']}
    expceted_models = {mid for mid, _ in _get_models()}
    try:
        user_email = request.json['user']['email']
        subscribe = request.json['register']
        query_json = request.json['query']
        assert set(query_json.keys()) == expected_query_keys, \
            (f'Did not get expected query keys: got {set(query_json.keys())} '
             f'not {expected_query_keys}')
        models = set(request.json.get('models'))
        assert models < expceted_models, \
            f'Got unexpected models: {models - expceted_models}'
    except (KeyError, AssertionError) as e:
        logger.exception(e)
        logger.error("Invalid query!")
        abort(Response(f'Invalid request: {str(e)}', 400))
    try:
        query = _make_query(query_json)
    except GroundingError as e:
        logger.exception(e)
        logger.error("Invalid grounding!")
        abort(Response(f'Invalid entity: {str(e)}', 400))

    is_test = 'test' in request.json or 'test' == request.json.get('tag')

    if is_test:
        logger.info('Test passed')
        res = {'result': 'test passed', 'ref': None}

    else:
        logger.info('Query submitted')
        try:
            result = qm.answer_immediate_query(
                user_email, query, models, subscribe)
        except Exception as e:
            logger.exception(e)
            raise(e)
        logger.info('Answer to query received, responding to client.')
        res = {'result': result}

    logger.info('Result: %s' % str(res))
    return Response(json.dumps(res), mimetype='application/json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser('Run the EMMAA dashboard service.')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', default=5000, type=int)
    parser.add_argument('--preload', action='store_true')
    args = parser.parse_args()

    # TODO: make pre-loading available when running service via Gunicorn
    if args.preload and not GLOBAL_PRELOAD:
        # Load all the model configs
        models = _get_models()
        # Load all the model mamangers for queries
        for model, _ in models:
            load_model_manager_from_s3(model)

    print(app.url_map)  # Get all avilable urls and link them
    app.run(host=args.host, port=args.port)
