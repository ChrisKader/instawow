from __future__ import annotations

from enum import Enum
from functools import partial, update_wrapper
from itertools import chain, islice
from operator import itemgetter
from textwrap import fill
from typing import TYPE_CHECKING
from typing import Any, Callable, Generator, Iterable, List, Optional, Sequence, Tuple, Union

import click

from . import __version__
from .config import Config
from . import exceptions as E
from .manager import CliManager, prepare_db_session
from .models import Pkg, PkgFolder, is_pkg
from .resolvers import Strategies, Defn
from .utils import TocReader, bucketise, cached_property, is_outdated, setup_logging, bbegone


class Symbols(str, Enum):

    SUCCESS = click.style('✓', fg='green')
    FAILURE = click.style('✗', fg='red')
    WARNING = click.style('!', fg='blue')

    @classmethod
    def from_result(cls, result: E.ManagerResult) -> Symbols:
        if isinstance(result, E.InternalError):
            return cls.WARNING
        elif isinstance(result, E.ManagerError):
            return cls.FAILURE
        return cls.SUCCESS

    def __str__(self) -> str:
        return self.value


class Report:

    def __init__(self, results: Sequence[Tuple[str, E.ManagerResult]],
                 filter_fn: Callable = (lambda _: True)) -> None:
        self.results = results
        self.filter_fn = filter_fn

    @property
    def code(self) -> int:
        return any(r for _, r in self.results
                   if (isinstance(r, (E.ManagerError, E.InternalError))
                       and self.filter_fn(r)))

    def __str__(self) -> str:
        return '\n'.join(
            (f'{Symbols.from_result(r)} {click.style(str(a), bold=True)}\n'
             f'  {r.message}')
            for a, r in self.results
            if self.filter_fn(r)
            )

    def generate(self) -> None:
        report = str(self)
        if report:
            click.echo(report)

    def generate_and_exit(self) -> None:
        self.generate()
        ctx = click.get_current_context()
        ctx.exit(self.code)


def tabulate(rows: Iterable, *, show_index: bool = True) -> str:
    from texttable import Texttable, Texttable as c

    table = Texttable(max_width=0).set_deco(c.BORDER | c.HEADER | c.VLINES)
    if show_index:
        iter_rows = iter(rows)
        header = next(iter_rows)
        rows = [('', *header), *((i, *v) for i, v in enumerate(iter_rows, start=1))]
        table.set_cols_align(('r', *('l' for _ in header)))
    return table.add_rows(rows).draw()


def decompose_pkg_defn(ctx: click.Context, param: Any,
                       value: Union[str, List[str], Tuple[str, ...]], *,
                       raise_when_invalid: bool = True) -> Any:
    if isinstance(value, (list, tuple)):
        return list(bucketise(decompose_pkg_defn(ctx, param, v) for v in value))    # Remove dupes

    if ':' not in value:
        if raise_when_invalid:
            raise click.BadParameter(value)

        parts = ('*', value)
    else:
        for resolver in ctx.obj.m.resolvers.values():
            parts = resolver.decompose_url(value)
            if parts:
                break
        else:
            parts = value.partition(':')[::2]
    return Defn(*parts)


def decompose_pkg_defn_with_strat(ctx, param, value):
    defns = decompose_pkg_defn(ctx, param, [d for _, d in value])
    strategies = (Strategies[s] for s, _ in value)
    return [Defn(o, n, s) for (o, n, _), s in zip(defns, strategies)]


def get_pkg_from_substr(manager: CliManager, defn: Defn) -> Optional[Pkg]:
    pkg = manager.get(defn)
    pkg = pkg or (manager.db_session.query(Pkg).filter(Pkg.slug.contains(defn.name))
                  .order_by(Pkg.name).first())
    return pkg


def _pass_manager(f: Callable) -> Callable:
    def new_func(*args: Any, **kwargs: Any) -> Callable:
        return f(click.get_current_context().obj.m, *args, **kwargs)
    return update_wrapper(new_func, f)


class _FreeFormEpilogCommand(click.Command):

    def format_epilog(self, ctx, formatter):
        self.epilog(formatter)


def _make_install_epilog(formatter):
    with formatter.section('Strategies'):
        formatter.write_dl((s.name, s.value) for s in islice(Strategies, 1, None))


@click.group(context_settings={'help_option_names': ('-h', '--help')})
@click.version_option(__version__, prog_name='instawow')
@click.option('--debug',
              is_flag=True, default=False,
              help='Log more things.')
@click.pass_context
def main(ctx, debug):
    "Add-on manager for World of Warcraft."
    if not ctx.obj:
        @object.__new__
        class ManagerSingleton:
            @cached_property
            def manager(self) -> CliManager:
                while True:
                    try:
                        config = Config.read().write()
                    except FileNotFoundError:
                        ctx.invoke(write_config)
                    else:
                        break

                setup_logging(config.logger_dir, 'DEBUG' if debug else 'INFO')
                db_session = prepare_db_session(config)
                manager = CliManager(config, db_session)
                if is_outdated(manager):
                    click.echo(f'{Symbols.WARNING} instawow is out of date')
                return manager

            m = manager

        ctx.obj = ManagerSingleton


@main.command(cls=_FreeFormEpilogCommand, epilog=_make_install_epilog)
@click.option('--with-strategy', '-s', 'strategic_addons',
              multiple=True,
              type=(click.Choice([s.name for s in Strategies]), str),
              callback=decompose_pkg_defn_with_strat,
              metavar='<STRATEGY ADDON>...',
              help='A strategy followed by an add-on definition.  '
                   'Use this if you want to install an add-on with a '
                   'strategy other than the default one.  '
                   'Repeatable.')
@click.option('--replace', '-o',
              is_flag=True, default=False,
              help='Replace existing add-ons.')
@click.argument('addons',
                nargs=-1, callback=decompose_pkg_defn)
@_pass_manager
def install(manager, addons, strategic_addons, replace) -> None:
    "Install add-ons."
    deduped_addons = bucketise(chain(strategic_addons, addons), key=itemgetter(0, 1))
    values = [v for v, *_ in deduped_addons.values()]
    results = manager.install(values, replace)
    Report(results.items()).generate_and_exit()


@main.command()
@click.argument('addons',
                nargs=-1, callback=decompose_pkg_defn)
@_pass_manager
def update(manager, addons) -> None:
    "Update installed add-ons."
    if addons:
        values = addons
    else:
        values = [Defn(p.origin, p.slug) for p in manager.db_session.query(Pkg).all()]
    results = manager.update(values)
    # Hide package from output if up to date and ``update`` was invoked without args
    filter_fn = lambda r: addons or not isinstance(r, E.PkgUpToDate)
    Report(results.items(), filter_fn).generate_and_exit()


@main.command()
@click.argument('addons',
                nargs=-1, required=True, callback=decompose_pkg_defn)
@_pass_manager
def remove(manager, addons) -> None:
    "Uninstall add-ons."
    results = manager.remove(addons)
    Report(results.items()).generate_and_exit()


@main.command()
@_pass_manager
def reconcile(manager) -> None:
    "Reconcile add-ons."
    from .matchers import _Addon, match_toc_ids, match_dir_names, get_leftovers
    from .prompts import Choice, confirm, select, skip

    def _prompt(addons: Sequence[_Addon], pkgs: Sequence[Pkg]) -> Union[Tuple[()], Defn]:
        def create_choice(pkg):
            defn = Defn(pkg.origin, pkg.slug)
            title = [('', str(defn)),
                     ('', '=='),
                     ('class:hilite' if highlight_version else '', pkg.version),]
            return Choice(title, defn, pkg=pkg)

        # Highlight version if there's multiple of them
        highlight_version = len(bucketise(i.version for i in chain(addons, pkgs))) > 1
        choices = list(chain(map(create_choice, pkgs), (skip,)))
        addon = addons[0]
        # Use 'unsafe_ask' to let ^C bubble up
        selection = select(f'{addon.name} [{addon.version or "?"}]', choices).unsafe_ask()
        return selection

    def prompt(groups: Iterable[Tuple[Sequence[_Addon], Sequence[Any]]]) -> Generator[Defn, None, None]:
        for addons, results in groups:
            shortlist = list(filter(is_pkg, results))
            if shortlist:
                selection = _prompt(addons, shortlist)
                selection and (yield selection)

    def match_all():
        # Match in order of increasing heuristicitivenessitude
        for fn in (match_toc_ids,
                   match_dir_names,):
            leftovers = get_leftovers(manager)
            groups = manager.run(fn(manager, leftovers))
            yield list(prompt(groups))

    if not get_leftovers(manager):
        click.echo('No add-ons left to reconcile.')
        return

    click.echo('''\
- Use the arrow keys to navigate, <o> to open an
  add-on in your browser and enter to make a selection.
- Versions that differ from the installed version
  or differ between choices are highlighted in purple.
- instawow will do a first pass of all of your add-ons
  looking for source IDs in TOC files, e.g. X-Curse-Project-ID.
- If it is unable to reconcile all of your add-ons
  it will perform a second pass to match add-on folders
  against the CurseForge and WoWInterface catalogues.
- Selected add-ons will be reinstalled.\
''')
    for selections in match_all():
        if selections and confirm('Install selected add-ons?').unsafe_ask():
            results = manager.install(selections, replace=True)
            Report(results.items()).generate()
    click.echo('- Unreconciled add-ons can be listed with '
                '`instawow list-folders -e`.')


@main.command()
@click.option('--limit', '-l',
              default=5, type=click.IntRange(1, 20, clamp=True),
              help='A number to limit results to.')
@click.argument('search-terms',
                nargs=-1, required=True, callback=lambda _, __, v: ' '.join(v))
@click.pass_context
def search(ctx, limit, search_terms):
    "Search for add-ons."
    from .prompts import Choice, checkbox, confirm

    manager = ctx.obj.m

    pkgs = manager.run(manager.search(search_terms, limit))
    if pkgs:
        choices = [Choice(f'{p.name}  ({d}=={p.version})', d, pkg=p)
                   for d, p in pkgs.items()]
        selections = checkbox('Select add-ons to install', choices=choices).unsafe_ask()
        if selections and confirm('Install selected add-ons?').unsafe_ask():
            ctx.invoke(install, addons=selections)


@main.command('list')
@click.option('--column', '-c', 'columns',
              multiple=True,
              help='A field to show in a column.  Nested fields are '
                   'dot-delimited.  Repeatable.')
@click.option('--columns', '-C', 'print_columns',
              is_flag=True, default=False,
              help='Print a list of all possible column values.')
@click.option('--sort-by', '-s', 'sort_key',
              default='name',
              help='A key to sort the table by.  '
                   'You can chain multiple keys by separating them with a comma '
                   'just as you would in SQL, '
                   'e.g. `--sort-by="origin, date_published DESC"`.')
@_pass_manager
def list_installed(manager, columns, print_columns, sort_key) -> None:
    "List installed add-ons."
    from operator import attrgetter
    from sqlalchemy import inspect, text

    def format_columns(pkg):
        for column in columns:
            try:
                value = attrgetter(column)(pkg)
            except AttributeError:
                raise click.BadParameter(column, param_hint=['--column', '-c'])
            if column == 'folders':
                yield '\n'.join(f.name for f in value)
            elif column == 'options':
                yield f'strategy = {value.strategy}'
            elif column == 'description':
                yield fill(value, width=50, max_lines=3)
            else:
                yield value

    if print_columns:
        columns = [('field',),
                   *((c,) for c in (*inspect(Pkg).columns.keys(),
                                    *inspect(Pkg).relationships.keys()))]
        click.echo(tabulate(columns, show_index=False))
    else:
        pkgs = manager.db_session.query(Pkg).order_by(text(sort_key)).all()
        if pkgs:
            rows = [('add-on', *columns),
                    *((Defn(p.origin, p.slug), *format_columns(p)) for p in pkgs)]
            click.echo(tabulate(rows))


@main.command()
@click.option('--exclude-own', '-e',
              is_flag=True, default=False,
              help='Exclude folders managed by instawow.')
@click.option('--toc-entry', '-t', 'toc_entries',
              multiple=True,
              help='An entry to extract from the TOC.  Repeatable.')
@_pass_manager
def list_folders(manager, exclude_own, toc_entries) -> None:
    "List add-on folders."
    folders = {f for f in manager.config.addon_dir.iterdir() if f.is_dir()}
    if exclude_own:
        folders -= {manager.config.addon_dir / f.name
                    for f in manager.db_session.query(PkgFolder).all()}

    folder_tocs = ((n, n / f'{n.name}.toc') for n in folders)
    folder_readers = sorted((n, TocReader.from_path(t)) for n, t in folder_tocs if t.exists())
    if folder_readers:
        rows = [('folder',
                 *(f'[{e}]' for e in toc_entries)),
                *((n.name,
                  *(fill(t[e].value, width=50) for e in toc_entries))
                  for n, t in folder_readers)]
        click.echo(tabulate(rows))


@main.command()
@click.option('--toc-entry', '-t', 'toc_entries',
              multiple=True,
              help='An entry to extract from the TOC.  Repeatable.')
@click.argument('addon', callback=partial(decompose_pkg_defn,
                                          raise_when_invalid=False))
@_pass_manager
def info(manager, addon, toc_entries) -> None:
    "Show detailed add-on information."
    pkg = get_pkg_from_substr(manager, addon)
    if pkg:
        rows = {'name': pkg.name,
                'source': pkg.origin,
                'id': pkg.id,
                'slug': pkg.slug,
                'description': fill(bbegone(pkg.description), max_lines=5),
                'homepage': pkg.url,
                'version': pkg.version,
                'release date': pkg.date_published,
                'folders': '\n'.join(f.name for f in pkg.folders),
                'strategy': pkg.options.strategy}

        if toc_entries:
            for folder in pkg.folders:
                toc_reader = TocReader.from_path_name(manager.config.addon_dir / folder.name)
                rows.update({f'[{folder.name} {k}]': fill(toc_reader[k].value)
                             for k in toc_entries})
        click.echo(tabulate([(), *rows.items()], show_index=False))
    else:
        Report([(addon, E.PkgNotInstalled())]).generate_and_exit()


@main.command()
@click.argument('addon', callback=partial(decompose_pkg_defn,
                                          raise_when_invalid=False))
@_pass_manager
def visit(manager, addon) -> None:
    "Open an add-on's homepage in your browser."
    pkg = get_pkg_from_substr(manager, addon)
    if pkg:
        import webbrowser
        webbrowser.open(pkg.url)
    else:
        Report([(addon, E.PkgNotInstalled())]).generate_and_exit()


@main.command()
@click.argument('addon', callback=partial(decompose_pkg_defn,
                                          raise_when_invalid=False))
@_pass_manager
def reveal(manager, addon) -> None:
    "Open an add-on folder in your file manager."
    pkg = get_pkg_from_substr(manager, addon)
    if pkg:
        import webbrowser
        webbrowser.open((manager.config.addon_dir / pkg.folders[0].name).as_uri())
    else:
        Report([(addon, E.PkgNotInstalled())]).generate_and_exit()


@main.command()
def write_config() -> None:
    "Configure instawow."
    import os.path
    from prompt_toolkit.completion import PathCompleter, WordCompleter
    from prompt_toolkit.shortcuts import CompleteStyle, prompt
    from prompt_toolkit.validation import Validator

    prompt_ = partial(prompt, complete_style=CompleteStyle.READLINE_LIKE)

    class DirectoryCompleter(PathCompleter):
        def __init__(self, *args, **kwargs):
            super().__init__(*args,
                             expanduser=True, only_directories=True, **kwargs)

        def get_completions(self, document, complete_event):
            # Append slash to every completion
            for completion in super().get_completions(document, complete_event):
                completion.text += '/'
                yield completion

    def validate_addon_dir(value: str) -> bool:
        path = os.path.expanduser(value)
        return os.path.isdir(path) and os.access(path, os.W_OK)

    completer = DirectoryCompleter()
    validator = Validator.from_callable(validate_addon_dir,
                                        error_message='must be a writable directory')
    addon_dir = prompt_('Add-on directory: ',
                        completer=completer, validator=validator)

    game_flavours = ('retail', 'classic')
    completer = WordCompleter(game_flavours)
    validator = Validator.from_callable(game_flavours.__contains__,
                                        error_message='must be one of: retail, classic')
    game_flavour = prompt_('Game flavour: ',
                           completer=completer, validator=validator)

    config = Config(addon_dir=addon_dir, game_flavour=game_flavour).write()
    click.echo(f'Configuration written to: {config.config_file}')


@main.command()
@_pass_manager
def show_config(manager) -> None:
    "Show the active configuration."
    click.echo(manager.config.json(exclude=set()))


@main.group('weakauras-companion')
def _weakauras_group() -> None:
    "Manage your WeakAuras."


@_weakauras_group.command('build')
@click.option('--account', '-a',
              required=True,
              help='Your account name.  This is used to locate '
                   'the WeakAuras data file.')
@_pass_manager
def build_weakauras_companion(manager, account) -> None:
    "Build the WeakAuras Companion add-on."
    from .wa_updater import WaCompanionBuilder

    manager.run(WaCompanionBuilder(manager).build(account))


@_weakauras_group.command('list')
@click.option('--account', '-a',
              required=True,
              help='Your account name.  This is used to locate '
                   'the WeakAuras data file.')
@_pass_manager
def list_installed_wago_auras(manager, account) -> None:
    "List WeakAuras installed from Wago."
    from .wa_updater import WaCompanionBuilder

    aura_groups = WaCompanionBuilder(manager).extract_installed_auras(account)
    installed_auras = sorted((a.id, a.url, str(a.ignore_wago_update).lower())
                             for v in aura_groups.values()
                             for a in v
                             if not a.parent)
    click.echo(tabulate([('name', 'url', 'ignore updates'),
                         *installed_auras]))
