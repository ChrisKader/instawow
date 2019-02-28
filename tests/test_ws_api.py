
import asyncio
from multiprocessing import Process
from unittest.mock import patch

import aiohttp
import pytest

from instawow import api
from instawow.config import Config
from instawow.manager import WsManager
from instawow.models import PkgCoercer, PkgFolderCoercer, PkgOptionsCoercer


PORT = 55439


class FailRequest(api.Request):

    _name = 'fail'

    def prepare_response(self, manager):
        async def raise_():
            raise ValueError

        return raise_()


@pytest.fixture(scope='module', autouse=True)
def ws_server(tmp_path_factory):
    config = Config(config_dir=tmp_path_factory.mktemp(f'{__name__}_config'),
                    addon_dir=tmp_path_factory.mktemp(f'{__name__}_addons'))
    config.write()

    with patch.dict(api._REQUESTS, {FailRequest._name: FailRequest}):
        process = Process(daemon=True,
                          target=lambda: WsManager(config=config, loop=asyncio.new_event_loop()
                                                   ).serve(None, PORT))
        process.start()
        process.join(3)         # Effectively ``time.sleep(3)``
        yield


@pytest.fixture
@pytest.mark.asyncio
async def ws_client():
    async with aiohttp.ClientSession() as client, \
            client.ws_connect(f'ws://0.0.0.0:{PORT}') as ws:
        yield ws


@pytest.mark.asyncio
async def test_parse_error_error_response(ws_client):
    request = 'foo'
    response = {'jsonrpc': '2.0',
                'id': None,
                'error': {'code': -32700,
                          'message': 'request is not valid JSON',
                          'data': 'Expecting value: line 1 column 1 (char 0)'}}
    await ws_client.send_str('foo')
    assert (await ws_client.receive_json()) == response


@pytest.mark.asyncio
async def test_invalid_request_error_response(ws_client):
    request = []
    response = {'jsonrpc': '2.0',
                'id': None,
                'error': {'code': -32600,
                          'message': 'request is malformed',
                          'data': '[{"loc": ["__obj__"], "msg": "BaseRequest expected dict not '
                                  'list", "type": "type_error"}]'}}
    await ws_client.send_json(request)
    assert (await ws_client.receive_json()) == response


@pytest.mark.asyncio
async def test_method_not_found_error_response(ws_client):
    request = {'jsonrpc': '2.0',
               'id': 'test_method_not_found_error_response',
               'method': 'foo'}
    response = {'jsonrpc': '2.0',
                'id': 'test_method_not_found_error_response',
                'error': {'code': -32601,
                          'message': 'request method not found',
                          'data': None}}
    await ws_client.send_json(request)
    assert (await ws_client.receive_json()) == response


@pytest.mark.asyncio
async def test_invalid_params_error_response(ws_client):
    request = {'jsonrpc': '2.0',
               'id': 'test_invalid_params_error_response',
               'method': 'get',
               'params': {}}
    response = {'jsonrpc': '2.0',
                'id': 'test_invalid_params_error_response',
                'error': {'code': -32602,
                          'message': 'request params are invalid',
                          'data': '[{"loc": ["params", "uris"], "msg": "field required", '
                                  '"type": "value_error.missing"}]'}}
    await ws_client.send_json(request)
    assert (await ws_client.receive_json()) == response


@pytest.mark.asyncio
async def test_internal_error_error_response(ws_client):
    request = {'jsonrpc': '2.0',
               'id': 'test_internal_error_error_response',
               'method': FailRequest._name,
               'params': {}}
    response = {'jsonrpc': '2.0',
                'id': 'test_internal_error_error_response',
                'error': {'code': -32603,
                          'message': 'encountered internal error',
                          'data': None}}
    await ws_client.send_json(request)
    assert (await ws_client.receive_json()) == response


@pytest.mark.asyncio
async def test_any_manager_error_error_response(ws_client):
    request = {'jsonrpc': '2.0',
               'id': 'test_any_manager_error_error_response',
               'method': 'resolve',
               'params': {'uri': 'foo:bar', 'resolution_strategy': 'canonical'}}
    response = {'jsonrpc': '2.0',
                'id': 'test_any_manager_error_error_response',
                'error': {'code': 10007,
                          'message': 'package origin is invalid',
                          'data': None}}
    await ws_client.send_json(request)
    assert (await ws_client.receive_json()) == response


@pytest.mark.asyncio
async def test_get_method_not_installed_response(ws_client):
    request = {'jsonrpc': '2.0',
               'id': 'test_get_method_not_installed_response',
               'method': 'get',
               'params': {'uris': ['curse:molinari']}}

    await ws_client.send_json(request)
    response = await ws_client.receive_json()
    assert response['id'] == request['id']
    assert api.SuccessResponse.parse_obj(response).result == [None]


@pytest.mark.asyncio
async def test_resolve_method_response(ws_client):
    request = {'jsonrpc': '2.0',
               'id': 'test_resolve_method_response',
               'method': 'resolve',
               'params': {'uri': 'curse:molinari', 'resolution_strategy': 'canonical'}}

    await ws_client.send_json(request)
    response = await ws_client.receive_json()
    assert response['id'] == request['id']
    assert api.SuccessResponse.parse_obj(response)
    PkgCoercer.parse_obj(response['result'])
    PkgOptionsCoercer.parse_obj(response['result']['options'])
    assert response['result']['folders'] == []


@pytest.mark.asyncio
async def test_resolve_method_with_url_in_uri_param_response(ws_client):
    request = {'jsonrpc': '2.0',
               'id': 'test_resolve_method_with_url_in_uri_param_response',
               'method': 'resolve',
               'params': {'uri': 'https://www.curseforge.com/wow/addons/molinari',
                          'resolution_strategy': 'canonical'}}

    await ws_client.send_json(request)
    response = await ws_client.receive_json()
    assert response['id'] == request['id']
    assert response['result']['origin'] == 'curse'
    assert response['result']['slug'] == 'molinari'


@pytest.mark.asyncio
async def test_install_method_response(ws_client):
    request = {'jsonrpc': '2.0',
               'id': 'test_install_method_response',
               'method': 'install',
               'params': {'uri': 'curse:molinari', 'resolution_strategy': 'canonical',
                          'overwrite': False}}

    await ws_client.send_json(request)
    response = api.SuccessResponse.parse_obj(await ws_client.receive_json())
    assert response.id == request['id']
    assert response.result is None


@pytest.mark.asyncio
async def test_get_method_response(ws_client):
    request = {'jsonrpc': '2.0',
               'id': 'test_get_method_response',
               'method': 'get',
               'params': {'uris': ['curse:molinari']}}

    await ws_client.send_json(request)
    response = await ws_client.receive_json()
    assert response['id'] == request['id']
    assert len(api.SuccessResponse.parse_obj(response).result) == 1
    PkgCoercer.parse_obj(response['result'][0])
    PkgOptionsCoercer.parse_obj(response['result'][0]['options'])
    assert len(response['result'][0]['folders']) > 0
    [PkgFolderCoercer.parse_obj(f) for f in response['result'][0]['folders']]


@pytest.mark.asyncio
async def test_remove_method_response(ws_client):
    request = {'jsonrpc': '2.0',
               'id': 'test_remove_method_response',
               'method': 'remove',
               'params': {'uri': 'curse:molinari'}}

    await ws_client.send_json(request)
    response = api.SuccessResponse.parse_obj(await ws_client.receive_json())
    assert response.id == request['id']
    assert response.result is None