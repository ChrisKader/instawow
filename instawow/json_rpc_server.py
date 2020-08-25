from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import contextmanager
from functools import partial
from itertools import repeat
import os
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    ClassVar,
    DefaultDict,
    Dict,
    Iterator,
    List,
    Optional as O,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    overload,
)

from aiohttp.web import Application, AppRunner
from aiohttp_json_rpc import JsonRpc, RpcError, RpcGenericServerDefinedError, RpcInvalidParamsError
from aiohttp_json_rpc.rpc import JsonRpcMethod
from pydantic import BaseModel, Field, ValidationError

from . import exceptions as E
from .config import Config
from .manager import Manager, init_web_client
from .matchers import get_folders, match_dir_names, match_toc_ids, match_toc_names
from .models import Pkg, PkgModel, is_pkg
from .resolvers import Defn, Strategies
from .utils import Literal, get_version, run_in_thread as t, uniq

if TYPE_CHECKING:
    _T = TypeVar('_T')
    ManagerWorkQueueItem = Tuple[asyncio.Future[Any], str, O[Callable[..., Awaitable[Any]]]]


API_VERSION = 0


class _InstawowError(RpcGenericServerDefinedError):
    ERROR_CODE = -32000
    MESSAGE = 'instawow encountered an error'


class _ConfigError(_InstawowError):
    ERROR_CODE = -32001
    MESSAGE = 'invalid configuration parameters'


@contextmanager
def _reraise_validation_error(error_class: Type[RpcError] = _InstawowError) -> Iterator[None]:
    try:
        yield
    except ValidationError as error:
        raise error_class(data=error.errors()) from error


JSONRPC = '2.0'


class Request(BaseModel):
    jsonrpc = JSONRPC
    method: str
    params: Union[List[Any], Dict[str, Any]]
    id: Union[None, int, str]


class BaseParams(BaseModel):
    _method: ClassVar[str]

    async def respond(self, managers: ManagerWorkQueue) -> Any:
        raise NotImplementedError


class _ProfileParamMixin(BaseModel):
    profile: str


class _DefnParamMixin(BaseModel):
    defns: List[Defn]


class WriteConfigParams(BaseParams):
    values: Dict[str, Any]
    _method = 'config.write'
    _result_type = type(None)

    @t
    def respond(self, managers: ManagerWorkQueue) -> _result_type:
        with _reraise_validation_error(_ConfigError):
            Config(**self.values).write()


class InferConfigParams(BaseParams):
    values: Dict[str, Any]
    _method = 'config.infer'
    _result_type = Config

    @t
    def respond(self, managers: ManagerWorkQueue) -> _result_type:
        with _reraise_validation_error(_ConfigError):
            return Config.infer(**self.values)


class ReadConfigParams(_ProfileParamMixin, BaseParams):
    _method = 'config.read'
    _result_type = Config

    @t
    def respond(self, managers: ManagerWorkQueue) -> _result_type:
        with _reraise_validation_error(_ConfigError):
            return Config.read(self.profile)


class DeleteConfigParams(_ProfileParamMixin, BaseParams):
    _method = 'config.delete'
    _result_type = type(None)

    async def respond(self, managers: ManagerWorkQueue) -> _result_type:
        async def delete_profile(manager: Manager):
            await t(manager.config.delete)()
            managers.unload(self.profile)

        await managers.run(self.profile, delete_profile)


class EnumerateProfilesParams(BaseParams):
    _method = 'config.enumerate'
    _result_type = List[str]

    @t
    def respond(self, managers: ManagerWorkQueue) -> _result_type:
        return [f for f in Config.list_profiles() if f != '__jsonrpc__']


class _Source(BaseModel):
    source: str
    name: str
    supported_strategies: Set[Strategies] = Field(alias='strategies')
    supports_rollback: bool

    class Config:
        orm_mode = True


class ListSourcesParams(_ProfileParamMixin, BaseParams):
    _method = 'sources.list'
    _result_type = List[_Source]

    async def respond(self, managers: ManagerWorkQueue) -> _result_type:
        manager = await managers.run(self.profile)
        return list(map(_Source.from_orm, manager.resolvers.values()))


class _PkgMeta(BaseModel):
    installed: bool = False
    damaged: bool = False
    new_version: O[str] = None


class _ListResult(BaseModel):
    __root__: List[Tuple[PkgModel, _PkgMeta]]


class ListInstalledParams(_ProfileParamMixin, BaseParams):
    check_for_updates: bool
    _method = 'list'
    _result_type = _ListResult

    async def respond(self, managers: ManagerWorkQueue) -> _result_type:
        manager = await managers.run(self.profile)
        installed_pkgs = manager.database.query(Pkg).all()
        damaged_pkgs = await managers.run(self.profile, t(Manager.find_damaged_pkgs))
        if self.check_for_updates:
            resolve_results = await managers.run(
                self.profile,
                partial(Manager.resolve, defns=list(map(Defn.from_pkg, installed_pkgs))),
            )
            outdated_pkgs = {
                p: r.version
                for p, r in zip(installed_pkgs, resolve_results.values())
                if is_pkg(r) and p.version != r.version
            }
        else:
            outdated_pkgs = {}
        pkg_objs = [
            (
                p,
                _PkgMeta(
                    installed=True, damaged=p in damaged_pkgs, new_version=outdated_pkgs.get(p)
                ),
            )
            for p in installed_pkgs
        ]
        pkg_objs = sorted(
            pkg_objs, key=lambda i: (not i[1].damaged, not i[1].new_version, i[0].name),
        )
        result = _ListResult.parse_obj(pkg_objs)
        return result


class SearchParams(_ProfileParamMixin, BaseParams):
    search_terms: str
    limit: int
    _method = 'search'
    _result_type = _ListResult

    async def respond(self, managers: ManagerWorkQueue) -> _result_type:
        results = await managers.run(
            self.profile,
            partial(Manager.search, search_terms=self.search_terms, limit=self.limit),
        )
        return _ListResult.parse_obj([(r, _PkgMeta()) for r in results.values()])


class ResolveUrisParams(_ProfileParamMixin, BaseParams):
    prospective_defns: List[str]
    _method = 'resolve_uris'
    _result_type = _ListResult

    async def respond(self, managers: ManagerWorkQueue) -> _result_type:
        manager = await managers.run(self.profile)
        source_name_pairs = filter(None, map(manager.pair_uri, self.prospective_defns))
        defns = [Defn(source=a, name=b) for a, b in source_name_pairs]
        results = await managers.run(self.profile, partial(Manager.resolve, defns=defns))
        return _ListResult.parse_obj([(r, _PkgMeta()) for r in results.values() if is_pkg(r)])


class _ModifyResult(BaseModel):
    __root__: List[
        Union[
            Tuple[Literal['success'], Tuple[PkgModel, _PkgMeta]],
            Tuple[Literal['failure', 'error'], str],
        ]
    ]


class InstallParams(_ProfileParamMixin, _DefnParamMixin, BaseParams):
    replace: bool
    _method = 'install'
    _result_type = _ModifyResult

    async def respond(self, managers: ManagerWorkQueue) -> _result_type:
        results = await managers.run(
            self.profile, partial(Manager.install, defns=self.defns, replace=self.replace)
        )
        return _ModifyResult.parse_obj(
            [
                (
                    r.kind,
                    (r.pkg, _PkgMeta(installed=True))
                    if isinstance(r, E.PkgInstalled)
                    else r.message,
                )
                for r in results.values()
            ]
        )


class UpdateParams(_ProfileParamMixin, _DefnParamMixin, BaseParams):
    _method = 'update'
    _result_type = _ModifyResult

    async def respond(self, managers: ManagerWorkQueue) -> _result_type:
        results = await managers.run(self.profile, partial(Manager.update, defns=self.defns))
        return _ModifyResult.parse_obj(
            [
                (
                    r.kind,
                    (r.new_pkg, _PkgMeta(installed=True))
                    if isinstance(r, E.PkgUpdated)
                    else r.message,
                )
                for r in results.values()
            ]
        )


class RemoveParams(_ProfileParamMixin, _DefnParamMixin, BaseParams):
    _method = 'remove'
    _result_type = _ModifyResult

    async def respond(self, managers: ManagerWorkQueue) -> _result_type:
        results = await managers.run(self.profile, partial(Manager.remove, defns=self.defns))
        return _ModifyResult.parse_obj(
            [
                (r.kind, (r.old_pkg, _PkgMeta()) if isinstance(r, E.PkgRemoved) else r.message)
                for r in results.values()
            ]
        )


class _AddonMatch(BaseModel):
    name: str
    version: str

    class Config:
        orm_mode = True


class _ReconcileResult(BaseModel):
    __root__: Tuple[List[Tuple[List[_AddonMatch], List[PkgModel]]], List[_AddonMatch]]


_matchers = {
    'toc_ids': match_toc_ids,
    'dir_names': match_dir_names,
    'toc_names': match_toc_names,
}


class ReconcileParams(_ProfileParamMixin, BaseParams):
    matcher: Literal['toc_ids', 'dir_names', 'toc_names']
    _method = 'reconcile'
    _result_type = _ReconcileResult

    async def respond(self, managers: ManagerWorkQueue) -> _result_type:
        leftovers = await managers.run(self.profile, t(get_folders))
        match_groups = await managers.run(
            self.profile, partial(_matchers[self.matcher], leftovers=leftovers)
        )
        uniq_defns = uniq(d for _, b in match_groups for d in b)
        resolve_results = await managers.run(
            self.profile, partial(Manager.resolve, defns=uniq_defns)
        )
        matches = [
            (a, list(filter(is_pkg, (resolve_results[i] for i in d)))) for a, d in match_groups
        ]
        leftovers = leftovers - frozenset(i for a, _ in match_groups for i in a)
        return _ReconcileResult.parse_obj((matches, leftovers))


class GetVersionParams(BaseParams):
    _method = 'meta.get_version'
    _result_type = str

    async def respond(self, managers: ManagerWorkQueue) -> _result_type:
        return get_version()


class _Response(BaseModel):
    jsonrpc = JSONRPC
    id: Union[None, int, str]


class SuccessResponse(_Response):
    result: Any


class ErrorResponse(_Response):
    error: Any


class ManagerWorkQueue:
    def __init__(self) -> None:
        asyncio.get_running_loop()  # Sanity check
        self._queue: asyncio.Queue[ManagerWorkQueueItem] = asyncio.Queue()
        self._managers: Dict[str, Manager] = {}
        self._locks: DefaultDict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._web_client = init_web_client()

    async def _jumpstart(self, profile: str) -> Manager:
        try:
            manager = self._managers[profile]
        except KeyError:
            async with self._locks['load manager']:
                with _reraise_validation_error(_ConfigError):
                    config = await t(Config.read)(profile)
                self._managers[profile] = manager = await t(Manager.from_config)(config)
            manager.web_client = self._web_client
            manager.locks = self._locks
        return manager

    async def listen(self) -> None:
        while True:
            (future, profile, coro_fn) = item = await self._queue.get()
            try:
                manager = await self._jumpstart(profile)
            except BaseException as error:
                future.set_exception(error)
            else:
                if coro_fn:

                    async def schedule(item: ManagerWorkQueueItem, manager: Manager):
                        future, _, coro_fn = item
                        try:
                            result = await coro_fn(manager)  # type: ignore
                        except BaseException as error:
                            future.set_exception(error)
                        else:
                            future.set_result(result)

                    asyncio.create_task(schedule(item, manager))
                else:
                    future.set_result(manager)

            self._queue.task_done()

    async def cleanup(self) -> None:
        await self._web_client.close()

    @overload
    async def run(self, profile: str, coro_fn: None = ...) -> Manager:
        ...

    @overload
    async def run(self, profile: str, coro_fn: Callable[..., Awaitable[_T]] = ...) -> _T:
        ...

    async def run(
        self, profile: str, coro_fn: O[Callable[..., Awaitable[_T]]] = None
    ) -> Union[Manager, _T]:
        future: asyncio.Future[Any] = asyncio.Future()
        self._queue.put_nowait((future, profile, coro_fn))
        return await asyncio.wait_for(future, None)

    def unload(self, profile: str) -> None:
        self._managers.pop(profile, None)


def _prepare_response(
    param_class: Type[BaseParams], managers: ManagerWorkQueue
) -> Tuple[str, JsonRpcMethod]:
    async def respond(request: Any) -> Any:
        with _reraise_validation_error(RpcInvalidParamsError):
            params = param_class.parse_obj(request.params)

        response = SuccessResponse(
            result=await params.respond(managers), id=request.msg.data['id']
        )
        return response.json()

    # Signal to ``JsonRpc`` that it should not attempt to encode the return value
    respond.raw_response = True
    return (param_class._method, JsonRpcMethod(respond))


async def create_app() -> Application:
    managers = ManagerWorkQueue()
    json_rpc = JsonRpc()
    json_rpc.methods = dict(
        map(
            _prepare_response,
            (
                WriteConfigParams,
                InferConfigParams,
                ReadConfigParams,
                DeleteConfigParams,
                EnumerateProfilesParams,
                ListSourcesParams,
                ListInstalledParams,
                SearchParams,
                ResolveUrisParams,
                InstallParams,
                UpdateParams,
                RemoveParams,
                ReconcileParams,
                GetVersionParams,
            ),
            repeat(managers),
        )
    )
    app = Application()
    app.router.add_route(
        '*',
        f'/v{API_VERSION}',
        json_rpc.handle_request,  # type: ignore  # aiohttp_json_rpc is untyped
    )

    async def on_shutdown(app: Application):
        listen.cancel()
        await managers.cleanup()

    listen = asyncio.create_task(managers.listen())
    app.on_shutdown.append(on_shutdown)
    return app


async def listen() -> None:
    "Fire up the server."
    loop = asyncio.get_running_loop()
    app_runner = AppRunner(await create_app())
    await app_runner.setup()
    # Placate the type checker - server is created in ``app_runner.setup``
    assert app_runner.server
    # By omitting the port ``loop.create_server`` will find a random available
    # port to bind to (equivalent to creating a socket on port 0)
    server = await loop.create_server(app_runner.server, '0.0.0.0')
    assert server.sockets
    (host, port) = server.sockets[0].getsockname()
    # We're writing the address to fd 3 just in case something seeps into
    # stdout or stderr.
    # The message needs to be in JSON for quote, unquote IPC with Electron
    message = f'{{"address": "ws://{host}:{port}/"}}\n'.encode()
    for fd in (1, 3):
        os.write(fd, message)
    try:
        # ``server_forever`` cleans up after the server when it's interrupted
        await server.serve_forever()
    finally:
        await app_runner.cleanup()
