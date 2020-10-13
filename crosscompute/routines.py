import re
import strictyaml
from distutils.version import StrictVersion
from os.path import dirname, join

from .constants import (
    DEFAULT_HOST,
    DEFAULT_VIEW_NAME,
    L,
    VIEW_NAMES)
from .exceptions import ToolDefinitionError
from .macros import get_environment_value


VARIABLE_TEXT_PATTERN = re.compile(r'({[^}]+})')
VARIABLE_ID_PATTERN = re.compile(r'{\s*([^}]+?)\s*}')


def get_crosscompute_host():
    return get_environment_value('CROSSCOMPUTE_HOST', DEFAULT_HOST)


def get_crosscompute_token():
    return get_environment_value('CROSSCOMPUTE_TOKEN', is_required=True)


def get_resource_url(host, resource_name, resource_id=None):
    url = host + '/' + resource_name
    if resource_id:
        url += '/' + resource_id
    return url + '.json'


def load_tool_definition(path):
    text = open(path, 'rt').read()
    dictionary = strictyaml.load(text).data
    if not isinstance(dictionary, dict):
        raise ToolDefinitionError({'definition': 'expected dictionary'})
    try:
        protocol_name = dictionary.pop('crosscompute')
        normalize = NORMALIZE_TOOL_DEFINITION_BY_PROTOCOL_NAME[protocol_name]
    except KeyError:
        raise ToolDefinitionError({
            'crosscompute': 'expected ' + ' or '.join(PROTOCOL_NAMES),
        })
    folder = dirname(path)
    return normalize(dictionary, folder)


def normalize_tool_definition_from_protocol_0_8_4(dictionary, folder):
    try:
        assert dictionary['kind'].lower() == 'tool'
    except (KeyError, AssertionError):
        raise ToolDefinitionError({'kind': 'expected tool'})
    d = {}
    if 'id' in dictionary:
        d['id'] = dictionary['id']
    if 'slug' in dictionary:
        d['slug'] = dictionary['slug']
    try:
        d['name'] = dictionary['name']
        d['version'] = dictionary['version']
    except KeyError as e:
        raise ToolDefinitionError({e.args[0]: 'required'})
    d['input'] = normalize_put_definition('input', dictionary, folder)
    d['output'] = normalize_put_definition('output', dictionary, folder)
    if 'tests' in dictionary:
        d['tests'] = normalize_tests_definition('tests', dictionary)
    if 'script' in dictionary:
        d['script'] = normalize_script_definition('script', dictionary)
    if 'environment' in dictionary:
        d['environment'] = normalize_environment_definition(
            'environment', dictionary)
    return d


def normalize_put_definition(key, dictionary, folder=None):
    d = {}

    try:
        put_dictionary = dictionary[key]
    except KeyError:
        L.warning(f'missing {key} definition')
        return d

    try:
        variable_dictionaries = put_dictionary['variables']
    except KeyError:
        L.warning(f'missing {key} variables definition')
        variable_dictionaries = []
    else:
        variable_dictionaries = normalize_variable_dictionaries(
            variable_dictionaries)
        d['variables'] = variable_dictionaries

    try:
        template_dictionaries = put_dictionary['templates']
    except KeyError:
        L.warning(f'missing {key} templates definition')
        template_dictionaries = []
    else:
        template_dictionaries = normalize_template_dictionaries(
            template_dictionaries, variable_dictionaries, folder)
        d['templates'] = template_dictionaries

    return d


def normalize_tests_definition(key, dictionary):
    try:
        raw_test_dictionaries = dictionary[key]
    except KeyError:
        L.warning(f'missing {key} definition')
        raw_test_dictionaries = []
    return normalize_test_dictionaries(raw_test_dictionaries)


def normalize_script_definition(key, dictionary):
    try:
        raw_script_dictionary = dictionary[key]
    except KeyError:
        L.warning(f'missing {key} definition')
        raw_script_dictionary = {}
    return normalize_script_dictionary(raw_script_dictionary)


def normalize_environment_definition(key, dictionary):
    try:
        raw_environment_dictionary = dictionary[key]
    except KeyError:
        L.warning(f'missing {key} definition')
        raw_environment_dictionary = {}
    return normalize_environment_dictionary(raw_environment_dictionary)


def normalize_blocks_definition(key, dictionary, variables, folder=None):
    if 'blocks' in dictionary:
        block_dictionaries = dictionary['blocks']
    elif 'path' in dictionary and folder is not None:
        template_path = join(folder, dictionary['path'])
        block_dictionaries = load_block_dictionaries(template_path)
    else:
        block_dictionaries = []
    return normalize_block_dictionaries(block_dictionaries, variables)


def normalize_variable_dictionaries(raw_variable_dictionaries):
    variable_dictionaries = []
    for raw_variable_dictionary in raw_variable_dictionaries:
        try:
            variable_id = raw_variable_dictionary['id']
            variable_path = raw_variable_dictionary['path']
        except KeyError as e:
            raise ToolDefinitionError({
                e.args[0]: 'required for each variable'})
        variable_dictionary = {
            'id': variable_id,
            'name': raw_variable_dictionary.get(
                'name', variable_id),
            'view': raw_variable_dictionary.get(
                'view', DEFAULT_VIEW_NAME),
            'path': variable_path,
        }
        variable_dictionaries.append(variable_dictionary)
    return variable_dictionaries


def normalize_template_dictionaries(
        raw_template_dictionaries, variable_dictionaries, folder=None):
    template_dictionaries = []
    for raw_template_dictionary in raw_template_dictionaries:
        try:
            template_id = raw_template_dictionary['id']
            template_name = raw_template_dictionary['name']
        except KeyError as e:
            raise ToolDefinitionError({
                e.args[0]: 'required for each template'})
        template_blocks = normalize_blocks_definition(
            'blocks', raw_template_dictionary, variable_dictionaries, folder)
        if not template_blocks:
            continue
        template_dictionaries.append({
            'id': template_id,
            'name': template_name,
            'blocks': template_blocks,
        })
    if not template_dictionaries:
        template_dictionaries.append({
            'id': 'generated',
            'name': 'Generated',
            'blocks': [{'id': _['id']} for _ in variable_dictionaries],
        })
    return template_dictionaries


def normalize_test_dictionaries(raw_test_dictionaries):
    return [{
        'id': _['id'],
        'name': _['name'],
        'path': _['path'],
    } for _ in raw_test_dictionaries]


def normalize_block_dictionaries(raw_block_dictionaries, variables):
    block_dictionaries = []
    for raw_block_dictionary in raw_block_dictionaries:
        if 'id' in raw_block_dictionary:
            block_dictionaries.append({'id': raw_block_dictionary['id']})
            continue
        block_dictionary = {}
        try:
            raw_view_name = raw_block_dictionary['view']
            raw_data_dictionary = raw_block_dictionary['data']
        except KeyError as e:
            raise ToolDefinitionError({
                e.args[0]: 'required for each block that lacks an id'})
        view_name = normalize_view_name(raw_view_name)
        data_dictionary = normalize_data_dictionary(
            raw_data_dictionary, view_name)
        block_dictionary['view'] = view_name
        block_dictionary['data'] = data_dictionary
        block_dictionaries.append(block_dictionary)
    return block_dictionaries


def normalize_data_dictionary(raw_data_dictionary, view_name):
    if not isinstance(raw_data_dictionary, dict):
        raise ToolDefinitionError({
            'data': 'must be a dictionary'})

    has_value = 'value' in raw_data_dictionary
    has_file = 'file' in raw_data_dictionary
    if not has_value and not has_file:
        raise ToolDefinitionError({
            'data': 'expected value or file'})

    data_dictionary = {}
    if has_value:
        data_dictionary['value'] = normalize_value_dictionary(
            raw_data_dictionary['value'], view_name)
    elif has_file:
        data_dictionary['file'] = normalize_file_dictionary(
            raw_data_dictionary['file'])
    return data_dictionary


def normalize_script_dictionary(raw_script_dictionary):
    uri = raw_script_dictionary['uri']
    folder = raw_script_dictionary['folder']
    command = raw_script_dictionary['command']
    return {
        'uri': uri,
        'folder': folder,
        'command': command,
    }


def normalize_environment_dictionary(raw_environment_dictionary):
    image = raw_environment_dictionary['image']
    processor = raw_environment_dictionary['processor']
    memory = raw_environment_dictionary['memory']
    return {
        'image': image,
        'processor': processor,
        'memory': memory,
    }


def normalize_value_dictionary(raw_value_dictionary, view_name=None):
    # TODO
    return raw_value_dictionary


def normalize_file_dictionary(raw_file_dictionary):
    # TODO
    return raw_file_dictionary


def normalize_view_name(raw_view_name):
    if not isinstance(raw_view_name, str):
        raise ToolDefinitionError({'view': 'must be a string'})

    view_name = raw_view_name.lower()
    if view_name not in VIEW_NAMES:
        raise ToolDefinitionError({'view': 'expected ' + ' or '.join(VIEW_NAMES)})
    return view_name


def load_block_dictionaries(template_path):
    try:
        template_text = open(template_path, 'rt').read()
    except IOError:
        raise ToolDefinitionError({
            'path': f'bad {template_path}'})
    return parse_block_dictionaries(template_text)


def parse_block_dictionaries(template_text):
    block_dictionaries = []
    for text in VARIABLE_TEXT_PATTERN.split(template_text):
        text = text.strip()
        if not text:
            continue
        match = VARIABLE_ID_PATTERN.match(text)
        if match:
            variable_id = match.group(1)
            block_dictionaries.append({
                'id': variable_id,
            })
        else:
            block_dictionaries.append({
                'view': 'markdown',
                'data': {
                    'value': text,
                },
            })
    return block_dictionaries


NORMALIZE_TOOL_DEFINITION_BY_PROTOCOL_NAME = {
    '0.8.4': normalize_tool_definition_from_protocol_0_8_4,
}
PROTOCOL_NAMES = sorted(
    NORMALIZE_TOOL_DEFINITION_BY_PROTOCOL_NAME, key=StrictVersion, reverse=True)
