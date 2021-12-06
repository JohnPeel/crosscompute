import json
import logging
import pandas as pd
import tomli
import yaml
from abc import ABC, abstractmethod
from configparser import ConfigParser
from os.path import basename, dirname, exists, getmtime, join, splitext
from pandas import Series, read_csv
from string import Template

from ..constants import (
    AUTOMATION_ROUTE,
    AUTOMATION_NAME,
    BATCH_ROUTE,
    FUNCTION_BY_NAME,
    PAGE_TYPE_NAMES,
    STYLE_ROUTE,
    VARIABLE_CACHE,
    VARIABLE_ID_PATTERN)
from ..exceptions import CrossComputeConfigurationError
from ..macros import (
    format_slug, get_environment_value, group_by, make_folder)
from .web import get_html_from_markdown


MAP_MAPBOX_CSS_URI = 'mapbox://styles/mapbox/dark-v10'
MAP_MAPBOX_JS_TEMPLATE = Template('''\
const $element_id = new mapboxgl.Map({
  container: '$element_id',
  style: '$style_uri',
  center: [$longitude, $latitude],
  zoom: $zoom,
})
$element_id.on('load', () => {
  $element_id.addSource('$element_id', {
    type: 'geojson',
    data: '$data_uri'})
  $element_id.addLayer({
    id: '$element_id',
    type: 'fill',
    source: '$element_id'})
})''')
MAP_PYDECK_SCREENGRID_JS_TEMPLATE = Template('''\
const layers = []
layers.push(new ScreenGridLayer({
  data: '$data_uri',
  getPosition: d => [d.longitude, d.latitude],
  getWeight: d => d.weight,
  opacity: $opacity,
}))
new deck.DeckGL({
  container: '$element_id',
  mapboxApiAccessToken: '$mapbox_token',
  mapStyle: '$style_uri',
  initialViewState: {
    longitude: $longitude,
    latitude: $latitude,
    zoom: $zoom,
  },
  controller: true,
  layers,
})
''')


def load_configuration(configuration_path):
    file_extension = splitext(configuration_path)[1]
    try:
        configuration_format = {
            '.cfg': 'ini',
            '.ini': 'ini',
            '.toml': 'toml',
            '.yaml': 'yaml',
            '.yml': 'yaml',
        }[file_extension]
    except KeyError:
        raise CrossComputeConfigurationError(
            f'{file_extension} configuration not supported')
    load_raw_configuration = {
        'ini': load_raw_configuration_ini,
        'toml': load_raw_configuration_toml,
        'yaml': load_raw_configuration_yaml,
    }[configuration_format]
    configuration = load_raw_configuration(configuration_path)
    configuration['folder'] = dirname(configuration_path) or '.'
    return validate_configuration(configuration)


def validate_configuration(configuration):
    for page_type_name in PAGE_TYPE_NAMES:
        page_configuration = configuration.get(page_type_name, {})
        for variable_definition in page_configuration.get('variables', []):
            try:
                variable_definition['id']
                variable_definition['view']
                variable_definition['path']
            except KeyError as e:
                raise CrossComputeConfigurationError(
                    '%s required for each variable', e)
    return configuration


def load_raw_configuration_ini(configuration_path):
    configuration = ConfigParser()
    configuration.read(configuration_path)
    return configuration


def load_raw_configuration_toml(configuration_path):
    with open(configuration_path, 'rt') as configuration_file:
        configuration = tomli.load(configuration_file)
    return configuration


def load_raw_configuration_yaml(configuration_path):
    with open(configuration_path, 'rt') as configuration_file:
        try:
            configuration = yaml.safe_load(configuration_file)
        except yaml.parser.ParserError as e:
            raise CrossComputeConfigurationError(e)
    return configuration


def get_automation_definitions(configuration):
    automation_definitions = []
    for automation_configuration in get_automation_configurations(
            configuration):
        if 'output' not in automation_configuration:
            continue
        automation_name = automation_configuration.get(
            'name', AUTOMATION_NAME.format(automation_index=0))
        automation_slug = automation_configuration.get(
            'slug', format_slug(automation_name))
        automation_uri = AUTOMATION_ROUTE.format(
            automation_slug=automation_slug)
        automation_configuration.update({
            'name': automation_name,
            'slug': automation_slug,
            'uri': automation_uri,
            'batches': get_batch_definitions(automation_configuration),
            'display': get_display_configuration(automation_configuration),
        })
        automation_definitions.append(automation_configuration)
    return automation_definitions


def get_automation_configurations(configuration):
    automation_configurations = []
    configurations = [configuration]
    while configurations:
        c = configurations.pop(0)
        folder = c['folder']
        for import_configuration in c.get('imports', []):
            if 'path' in import_configuration:
                path = import_configuration['path']
                automation_configuration = load_configuration(join(
                    folder, path))
            else:
                logging.error(
                    'path or folder or uri or name required for each import')
                continue
            automation_configuration['parent'] = c
            configurations.append(automation_configuration)
        automation_configurations.append(c)
    return automation_configurations


def get_batch_definitions(configuration):
    batch_definitions = []
    configuration_folder = configuration['folder']
    variable_definitions = get_raw_variable_definitions(
        configuration, 'input')
    for raw_batch_definition in configuration.get('batches', []):
        try:
            batch_definition = normalize_batch_definition(raw_batch_definition)
            if 'configuration' in raw_batch_definition:
                batch_configuration = raw_batch_definition['configuration']
                if 'path' in batch_configuration:
                    definitions = get_batch_definitions_from_path(join(
                        configuration_folder, batch_configuration['path'],
                    ), batch_definition, variable_definitions)
                # TODO: Support batch_configuration['uri']
                else:
                    raise CrossComputeConfigurationError(
                        'path expected for each batch configuration')
            else:
                definitions = [batch_definition]
        except CrossComputeConfigurationError as e:
            logging.error(e)
        batch_definitions.extend(definitions)
    return batch_definitions


def normalize_batch_definition(raw_batch_definition):
    try:
        batch_folder = get_scalar_text(raw_batch_definition, 'folder')
    except KeyError:
        raise CrossComputeConfigurationError('folder required for each batch')
    batch_name = get_scalar_text(raw_batch_definition, 'name', basename(
        batch_folder))
    batch_slug = get_scalar_text(raw_batch_definition, 'slug', '')
    return {
        'folder': batch_folder,
        'name': batch_name,
        'slug': batch_slug,
    }


def get_batch_definitions_from_path(
        path, batch_definition, variable_definitions):
    file_extension = splitext(path)[1]
    try:
        yield_data_by_id = {
            '.csv': yield_data_by_id_from_csv,
            '.txt': yield_data_by_id_from_txt,
        }[file_extension]
    except KeyError:
        raise CrossComputeConfigurationError(
            f'{file_extension} not supported for batch configuration')
    batch_folder = batch_definition['folder']
    batch_name = batch_definition['name']
    batch_slug = batch_definition['slug']
    batch_definitions = []
    for data_by_id in yield_data_by_id(path, variable_definitions):
        folder = format_text(batch_folder, data_by_id)
        name = format_text(batch_name, data_by_id)
        slug = format_text(
            batch_slug, data_by_id) if batch_slug else format_slug(name)
        batch_definitions.append(batch_definition | {
            'folder': folder, 'name': name, 'slug': slug,
            'uri': BATCH_ROUTE.format(batch_slug=slug),
            'data_by_id': data_by_id})
    return batch_definitions


def get_raw_variable_definitions(configuration, page_type_name):
    page_configuration = configuration.get(page_type_name, {})
    variable_definitions = page_configuration.get('variables', [])
    # for variable_definition in variable_definitions:
    #    variable_definition['type'] = page_type_name
    return variable_definitions


def get_all_variable_definitions(configuration, page_type_name):
    variable_definitions = get_raw_variable_definitions(
        configuration, page_type_name).copy()
    for type_name in PAGE_TYPE_NAMES[:2]:
        if type_name == page_type_name:
            continue
        variable_definitions.extend(get_raw_variable_definitions(
            configuration, type_name))
    return variable_definitions


def get_template_texts(configuration, page_type_name):
    template_texts = []
    folder = configuration['folder']
    page_configuration = configuration.get(page_type_name, {})
    for template_definition in page_configuration.get('templates', []):
        try:
            template_path = template_definition['path']
        except KeyError:
            logging.error('path required for each template')
            continue
        try:
            path = join(folder, template_path)
            template_file = open(path, 'rt')
        except OSError:
            logging.error(f'{path} does not exist or is not accessible')
            continue
        template_text = template_file.read().strip()
        if not template_text:
            continue
        template_texts.append(template_text)
    if not template_texts:
        variable_definitions = get_raw_variable_definitions(
            configuration, page_type_name)
        variable_ids = [_['id'] for _ in variable_definitions if 'id' in _]
        template_texts = [' '.join('{' + _ + '}' for _ in variable_ids)]
    return template_texts


def get_css_uris(configuration):
    has_parent = 'parent' in configuration
    display_configuration = configuration.get('display', {})
    css_uris = []
    for style_definition in display_configuration.get('styles', []):
        style_uri = style_definition['uri']
        is_relative = r'//' not in style_uri
        if has_parent and is_relative:
            style_uri = configuration['uri'] + style_uri
        css_uris.append(style_uri)
    return css_uris


def get_display_configuration(configuration):
    folder = configuration['folder']
    display_configuration = configuration.get('display', {})
    for style_definition in display_configuration.get('styles', []):
        uri = style_definition.get('uri', '').strip()
        path = style_definition.get('path', '').strip()
        if not uri and not path:
            logging.error('uri or path required for each style')
            continue
        if path:
            if not exists(join(folder, path)):
                logging.error('style not found at path %s', path)
            style_definition['uri'] = STYLE_ROUTE.format(
                style_path=path)
    return display_configuration


def get_scalar_text(configuration, key, default=None):
    value = configuration.get(key, default)
    if value is None:
        raise KeyError
    if isinstance(value, dict):
        logging.error(
            'quotes should surround text that begins '
            'with a variable id')
        variable_id = list(value.keys())[0]
        value = '{%s}' % variable_id
    return value


def prepare_batch_folder(
        batch_definition, variable_definitions, configuration_folder):
    batch_folder = batch_definition['folder']
    input_folder = make_folder(join(
        configuration_folder, batch_folder, 'input'))
    variable_definitions_by_path = group_by(variable_definitions, 'path')
    data_by_id = batch_definition.get('data_by_id', {})
    for path, variable_definitions in variable_definitions_by_path.items():
        input_path = join(input_folder, path)
        if exists(input_path):
            continue
        file_extension = splitext(path)[1]
        variable_data_by_id = get_variable_data_by_id(
            variable_definitions, data_by_id)
        if file_extension == '.json':
            json.dump(open(input_path, 'wt'), variable_data_by_id)
        elif file_extension == '.csv':
            Series(variable_data_by_id).to_csv(input_path, header=False)
        elif len(variable_data_by_id) > 1:
            raise CrossComputeConfigurationError(
                f'{file_extension} does not support multiple variables')
        else:
            variable_data = list(variable_data_by_id.values())[0]
            open(input_path, 'wt').write(variable_data)
    return batch_folder


def get_variable_data_by_id(variable_definitions, data_by_id):
    variable_data_by_id = {}
    for variable_definition in variable_definitions:
        variable_id = variable_definition['id']
        if None in data_by_id:
            variable_data = data_by_id[None]
        else:
            try:
                variable_data = data_by_id[variable_id]
            except KeyError:
                raise CrossComputeConfigurationError(
                    '%s not defined in batch configuration', variable_id)
        variable_data_by_id[variable_id] = variable_data
    return variable_data_by_id


def format_text(text, data_by_id):
    if not data_by_id:
        return text
    if None in data_by_id:
        f = data_by_id[None]
    else:
        def f(match):
            matching_text = match.group(0)
            expression_text = match.group(1)
            if expression_text in data_by_id:
                text = data_by_id[expression_text]
            elif '|' in expression_text:
                expression_terms = expression_text.split('|')
                variable_id = expression_terms[0].strip()
                try:
                    text = data_by_id[variable_id]
                except KeyError:
                    logging.warning(
                        '%s missing in batch configuration', variable_id)
                    return matching_text
                text = apply_functions(
                    text, expression_terms[1:], FUNCTION_BY_NAME)
            else:
                logging.warning(
                    '%s missing in batch configuration', expression_text)
                return matching_text
            return str(text)
    return VARIABLE_ID_PATTERN.sub(f, text)


def apply_functions(value, function_names, function_by_name):
    for function_name in function_names:
        function_name = function_name.strip()
        if not function_name:
            continue
        try:
            f = function_by_name[function_name]
        except KeyError:
            logging.error('%s not supported', function_name)
            continue
        value = f(value)
    return value


def yield_data_by_id_from_txt(path, variable_definitions):
    if len(variable_definitions) > 1:
        raise CrossComputeConfigurationError(
            'use .csv to configure multiple variables')

    try:
        variable_id = variable_definitions[0]['id']
    except IndexError:
        variable_id = None

    try:
        with open(path, 'rt') as batch_configuration_file:
            for line in batch_configuration_file:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                yield {variable_id: line}
    except OSError:
        raise CrossComputeConfigurationError('%s path not found', path)


def yield_data_by_id_from_csv(path, variable_definitions):
    table = read_csv(path)
    for index, row in table.iterrows():
        yield dict(row)


def get_variable_view_class(variable_definition):
    view_name = variable_definition['view']
    try:
        # TODO: Load using importlib.metadata
        VariableView = {
            'number': NumberView,
            'image': ImageView,
            'map-mapbox': MapMapboxView,
            'map-pydeck-screengrid': MapPyDeckScreenGridView,
            'markdown': MarkdownView,
        }[view_name]
    except KeyError:
        logging.error(f'{view_name} view not installed')
        return NullView
    return VariableView


class VariableView(ABC):

    is_asynchronous = False

    @abstractmethod
    def render(
            self, type_name, variable_index, variable_id, variable_data=None,
            variable_path=None, variable_configuration=None,
            request_path=None):
        return {
            'css_uris': [],
            'js_uris': [],
            'body_text': '',
            'js_texts': [],
        }


class NullView(VariableView):

    def render(
            self, type_name, variable_index, variable_id, variable_data=None,
            variable_path=None, variable_configuration=None,
            request_path=None):
        return {
            'css_uris': [],
            'js_uris': [],
            'body_text': '',
            'js_texts': [],
        }


class NumberView(VariableView):

    def render(
            self, type_name, variable_index, variable_id, variable_data=None,
            variable_path=None, variable_configuration=None,
            request_path=None):
        element_id = f'v{variable_index}'
        body_text = (
            f'<input id="{element_id}" '
            f'class="{type_name} number {variable_id}" '
            f'value="{variable_data}" type="number">')
        return {
            'css_uris': [],
            'js_uris': [],
            'body_text': body_text,
            'js_texts': [],
        }


class ImageView(VariableView):

    is_asynchronous = True

    def render(
            self, type_name, variable_index, variable_id, variable_data=None,
            variable_path=None, variable_configuration=None,
            request_path=None):
        # TODO: Support type_name == 'input'
        element_id = f'v{variable_index}'
        data_uri = request_path + '/' + variable_path
        body_text = (
            f'<img id="{element_id}" '
            f'class="{type_name} image {variable_id}" '
            f'src="{data_uri}">'
        )
        return {
            'css_uris': [],
            'js_uris': [],
            'body_text': body_text,
            'js_texts': [],
        }


class MapMapboxView(VariableView):

    is_asynchronous = True
    css_uris = [
        'https://api.mapbox.com/mapbox-gl-js/v2.6.0/mapbox-gl.css',
    ]
    js_uris = [
        'https://api.mapbox.com/mapbox-gl-js/v2.6.0/mapbox-gl.js',
    ]

    def render(
            self, type_name, variable_index, variable_id, variable_data=None,
            variable_path=None, variable_configuration=None,
            request_path=None):
        element_id = f'v{variable_index}'
        body_text = (
            f'<div id="{element_id}" '
            f'class="{type_name} map-mapbox {variable_id}"></div>')
        mapbox_token = get_environment_value('MAPBOX_TOKEN', '')
        js_texts = [
            f"mapboxgl.accessToken = '{mapbox_token}'",
            MAP_MAPBOX_JS_TEMPLATE.substitute({
                'element_id': element_id,
                'data_uri': request_path + '/' + variable_path,
                'style_uri': variable_configuration.get(
                    'style', MAP_MAPBOX_CSS_URI),
                'longitude': variable_configuration.get('longitude', 0),
                'latitude': variable_configuration.get('latitude', 0),
                'zoom': variable_configuration.get('zoom', 0),
            }),
        ]
        # TODO: Allow specification of preserveDrawingBuffer
        return {
            'css_uris': self.css_uris,
            'js_uris': self.js_uris,
            'body_text': body_text,
            'js_texts': js_texts,
        }


class MapPyDeckScreenGridView(VariableView):

    is_asynchronous = True
    css_uris = [
        'https://api.mapbox.com/mapbox-gl-js/v2.6.0/mapbox-gl.css',
    ]
    js_uris = [
        'https://unpkg.com/deck.gl@^8.0.0/dist.min.js',
        'https://api.mapbox.com/mapbox-gl-js/v2.6.0/mapbox-gl.js',
    ]

    def render(
            self, type_name, variable_index, variable_id, variable_data=None,
            variable_path=None, variable_configuration=None,
            request_path=None):
        element_id = f'v{variable_index}'
        body_text = (
            f'<div id="{element_id}" '
            f'class="{type_name} map-pydeck-screengrid {variable_id}"></div>')
        mapbox_token = get_environment_value('MAPBOX_TOKEN', '')
        js_texts = [
            MAP_PYDECK_SCREENGRID_JS_TEMPLATE.substitute({
                'data_uri': request_path + '/' + variable_path,
                'opacity': variable_configuration.get('opacity', 0.5),
                'element_id': element_id,
                'mapbox_token': mapbox_token,
                'style_uri': variable_configuration.get(
                    'style', MAP_MAPBOX_CSS_URI),
                'longitude': variable_configuration.get('longitude', 0),
                'latitude': variable_configuration.get('latitude', 0),
                'zoom': variable_configuration.get('zoom', 0),
            }),
        ]
        return {
            'css_uris': self.css_uris,
            'js_uris': self.js_uris,
            'body_text': body_text,
            'js_texts': js_texts,
        }


class MarkdownView(VariableView):

    def render(
            self, type_name, variable_index, variable_id, variable_data=None,
            variable_path=None, variable_configuration=None,
            request_path=None):
        element_id = f'v{variable_index}'
        body_text = (
            f'<span id="{element_id}" '
            f'class="{type_name} markdown {variable_id}">'
            f'{get_html_from_markdown(variable_data)}</span>')
        return {
            'css_uris': [],
            'js_uris': [],
            'body_text': body_text,
            'js_texts': [],
        }


def load_data(path, variable_id):
    new_time = getmtime(path)
    key = path, variable_id
    if key in VARIABLE_CACHE:
        old_time, variable_value = VARIABLE_CACHE[key]
        if old_time == new_time:
            return variable_value
    file_extension = splitext(path)[1]
    try:
        with open(path, 'rt') as file:
            if file_extension in ['.json', '.csv']:
                if file_extension == '.json':
                    value_by_id = json.load(file)
                elif file_extension == '.csv':
                    value_by_id = pd.read_csv(
                        file, header=None, index_col=0, squeeze=True)
                for i, v in value_by_id.items():
                    VARIABLE_CACHE[(path, i)] = new_time, v
                value = value_by_id[variable_id]
            else:
                value = file.read()
    except (OSError, KeyError, json.JSONDecodeError, pd.errors.ParserError):
        value = ''
    VARIABLE_CACHE[(path, variable_id)] = new_time, value
    return value
