
from collections import namedtuple
from functools import reduce
from textwrap import fill
from typing import List, Tuple
import webbrowser

import click
from sqlalchemy import inspect
from texttable import Texttable

from . import __version__
from .config import UserConfig
from .constants import MESSAGES
from .manager import Manager
from .models import Pkg, PkgFolder
from .utils import TocReader


_CONTEXT_SETTINGS = {'help_option_names': ['-h', '--help']}
_SEP = ':'

_parts = namedtuple('Parts', 'origin id_or_slug')


def _tabulate(rows: List[Tuple[str]], *,
              head: Tuple[str]=(), show_index: bool=True) -> str:
    table = Texttable(max_width=0)
    table.set_chars('   -')
    table.set_deco(Texttable.HEADER | Texttable.VLINES)

    if show_index:
        table.set_cols_align(('r', *('l' for _ in rows[0])))
        head = ('', *head)
        rows = [(i, *v) for i, v in enumerate(rows, start=1)]
    table.add_rows([head, *rows])
    return table.draw()


def _compose_addon_defn(val):
    try:
        origin, slug = val.origin, val.slug
    except AttributeError:
        origin, slug = val
    return _SEP.join((origin, slug))


def _decompose_addon_defn(ctx, param, value):
    if isinstance(value, tuple):
        return [_decompose_addon_defn(ctx, param, v) for v in value]
    for resolver in ctx.obj.resolvers.values():
        parts = resolver.decompose_url(value)
        if parts:
            parts = _parts(*parts)
            break
    else:
        if _SEP not in value:
            raise click.BadParameter(value)
        parts = value.partition(_SEP)
        parts = _parts(parts[0], parts[-1])
    return _compose_addon_defn(parts), parts


def _init():
    addon_dir = UserConfig.default_addon_dir
    while True:
        try:
            UserConfig(addon_dir=addon_dir).mk_app_dirs().write()
        except ValueError:
            if addon_dir:
                click.echo(f'{addon_dir!r} not found')
            addon_dir = click.prompt('Please enter the path to your add-on folder')
        else:
            break

init = click.Command(name='instawow-init', callback=_init,
                     context_settings=_CONTEXT_SETTINGS)


@click.group(context_settings=_CONTEXT_SETTINGS)
@click.version_option(__version__)
@click.pass_context
def main(ctx):
    """Add-on manager for World of Warcraft."""
    if not ctx.obj:
        while True:
            try:
                config = UserConfig.read()
            except (FileNotFoundError, ValueError):
                _init()
            else:
                break
        ctx.obj = manager = Manager(config=config)
        ctx.call_on_close(manager.close)

cli = main


@main.command()
@click.argument('addons', nargs=-1, callback=_decompose_addon_defn)
@click.option('--strategy', '-s',
              type=click.Choice(['canonical', 'latest']),
              default='canonical',
              help="Whether to install the latest published version "
                   "('canonical') or the very latest upload ('latest').")
@click.option('--overwrite', '-o',
              is_flag=True, default=False,
              help='Whether to overwrite existing add-ons.')
@click.pass_obj
def install(manager, addons, overwrite, strategy):
    """Install add-ons."""
    for addon, result in zip((d for d, _ in addons),
                             manager.install_many((*p, strategy, overwrite)
                                                  for _, p in addons)):
        try:
            if isinstance(result, Exception):
                raise result
        except manager.PkgAlreadyInstalled:
            click.echo(MESSAGES['install_failure__installed'](id=addon))
        except manager.PkgOriginInvalid:
            click.echo(MESSAGES['install_failure__invalid_origin'](id=addon))
        except manager.PkgNonexistent:
            click.echo(MESSAGES['any_failure__non_existent'](id=addon))
        except manager.PkgConflictsWithPreexisting:
            click.echo(MESSAGES['install_failure__preexisting_'
                                'folder_conflict'](id=addon))
        except manager.PkgConflictsWithInstalled as e:
            click.echo(MESSAGES['any_failure__installed_folder_conflict'](
                id=addon, other=_compose_addon_defn(e.conflicting_pkg)))
        else:
            click.echo(MESSAGES['install_success'](id=addon, version=result.version))


@main.command()
@click.argument('addons', nargs=-1, callback=_decompose_addon_defn)
@click.pass_obj
def update(manager, addons):
    """Update installed add-ons."""
    if not addons:
        addons = [(_compose_addon_defn(p), (p.origin, p.id))
                  for p in manager.db.query(Pkg).order_by(Pkg.slug).all()]
    for addon, result in zip((d for d, _ in addons),
                             manager.update_many(p for _, p in addons)):
        try:
            if isinstance(result, Exception):
                raise result
        except manager.PkgNonexistent:
            click.echo(MESSAGES['any_failure__non_existent'](id=addon))
        except manager.PkgNotInstalled:
            click.echo(MESSAGES['any_failure__not_installed'](id=addon))
        except manager.PkgConflictsWithInstalled as e:
            click.echo(MESSAGES['any_failure__installed_folder_conflict'](
                id=addon, other=_compose_addon_defn(e.conflicting_pkg)))
        except manager.PkgUpToDate:
            pass
        else:
            click.echo(MESSAGES['update_success'](id=addon,
                                                  old_version=result[0].version,
                                                  new_version=result[1].version))


@main.command()
@click.argument('addons', nargs=-1, callback=_decompose_addon_defn)
@click.pass_obj
def remove(manager, addons):
    """Uninstall add-ons."""
    for addon, parts in addons:
        try:
            manager.remove(*parts)
        except manager.PkgNotInstalled:
            click.echo(MESSAGES['any_failure__not_installed'](id=addon))
        else:
            click.echo(MESSAGES['remove_success'](id=addon))


@main.group('list')
def list_():
    """List add-ons."""


@list_.command()
@click.option('--column', '-c',
              multiple=True,
              help='A field to show in a column.  Nested fields are '
                   'dot-delimited.  Can be repeated.')
@click.option('--columns', '-C',
              is_flag=True, default=False,
              help='Whether to print a list of all possible column values.')
@click.pass_obj
def installed(manager, column, columns):
    """List installed add-ons."""
    def _format_columns(pkg, columns):
        def _parse_field(name, value):
            if name == 'folders':
                value = '\n'.join(f.path.name for f in value)
            elif name == 'description':
                value = fill(value, width=40)
            return value

        return (_parse_field(c, reduce(getattr, [pkg] + c.split('.')))
                for c in columns)

    if columns:
        # TODO: include relationships in output
        click.echo(_tabulate([(c,) for c in inspect(Pkg).columns.keys()],
                             head=('field',)))
    else:
        pkgs = manager.db.query(Pkg).order_by(Pkg.slug).all()
        if not pkgs:
            return
        try:
            click.echo(_tabulate([(_compose_addon_defn(p),
                                   *_format_columns(p, column)) for p in pkgs],
                                 head=('add-on', *column)))
        except AttributeError as e:
            raise click.BadParameter(e.args)


@list_.command()
@click.pass_obj
def outdated(manager):
    """List outdated add-ons."""
    def _is_not_up_to_date(p, r):
        try:
            if isinstance(r, Exception):
                raise r
        except manager.PkgNonexistent:
            return False
        else:
            return p.file_id != r.file_id

    installed = manager.db.query(Pkg).order_by(Pkg.slug).all()
    new = manager.resolve_many((p.origin, p.id, p.options.strategy)
                               for p in installed)
    outdated = [(p, r) for p, r in zip(installed, new) if _is_not_up_to_date(p, r)]
    if outdated:
        click.echo(_tabulate([(_compose_addon_defn(r),
                               p.version, r.version, r.options.strategy)
                              for p, r in outdated],
                             head=('add-on', 'current version',
                                   'new version', 'strategy')))


@list_.command()
@click.pass_obj
def preexisting(manager):
    """List add-ons not installed by instawow."""
    folders = {f.name
               for f in manager.config.addon_dir.iterdir() if f.is_dir()} - \
              {f.path.name for f in manager.db.query(PkgFolder).all()}
    folders = ((n, manager.config.addon_dir/n/f'{n}.toc') for n in folders)
    folders = {(n, TocReader(t)) for n, t in folders if t.exists()}
    if folders:
        click.echo(_tabulate([(n,
                               t['X-Curse-Project-ID'].value,
                               t['X-Curse-Packaged-Version', 'X-Packaged-Version',
                                 'Version'].value) for n, t in sorted(folders)],
                             head=('folder', 'curse id or slug', 'version')))


@main.command('set')
@click.argument('addons', nargs=-1, callback=_decompose_addon_defn)
@click.option('--strategy', '-s',
              type=click.Choice(['canonical', 'latest']),
              help="Whether to fetch the latest published version "
                   "('canonical') or the very latest upload ('latest').")
@click.pass_obj
def set_(manager, addons, strategy):
    """Modify add-on settings."""
    for addon in addons:
        pkg = addon[1]
        pkg = Pkg.unique(pkg.origin, pkg.id_or_slug, manager.db)
        if pkg:
            pkg.options.strategy = strategy
            manager.db.commit()
            click.echo(MESSAGES['set_success'](id=addon[0], var='strategy',
                                               new_strategy=strategy))
        else:
            click.echo(MESSAGES['any_failure__not_installed'](id=addon[0]))


@main.command()
@click.argument('addon', callback=_decompose_addon_defn)
@click.pass_obj
def info(manager, addon):
    """Display installed add-on information."""
    pkg = addon[1]
    pkg = Pkg.unique(pkg.origin, pkg.id_or_slug, manager.db)
    if pkg:
        rows = [('origin', pkg.origin),
                ('slug', pkg.slug),
                ('name', click.style(pkg.name, bold=True)),
                ('id', pkg.id),
                ('description', fill(pkg.description, max_lines=5)),
                ('homepage', click.style(pkg.url, underline=True)),
                ('version', pkg.version),
                ('release date', pkg.date_published),
                ('folders',
                 '\n'.join([str(pkg.folders[0].path.parent)] +
                           [' ├─ ' + f.path.name for f in pkg.folders[:-1]] +
                           [' └─ ' + pkg.folders[-1].path.name])),
                ('strategy', pkg.options.strategy),]
        click.echo(_tabulate(rows, show_index=False))
    else:
        click.echo(MESSAGES['any_failure__not_installed'](id=addon[0]))


@main.command()
@click.argument('addon', callback=_decompose_addon_defn)
@click.pass_obj
def hearth(manager, addon):
    """Open the add-on's homepage in your browser."""
    pkg = addon[1]
    pkg = Pkg.unique(pkg.origin, pkg.id_or_slug, manager.db)
    if pkg:
        webbrowser.open(pkg.url)
    else:
        click.echo(MESSAGES['any_failure__not_installed'](id=addon[0]))


@main.command()
@click.argument('addon', callback=_decompose_addon_defn)
@click.pass_obj
def reveal(manager, addon):
    """Open the add-on folder in your file manager."""
    pkg = addon[1]
    pkg = Pkg.unique(pkg.origin, pkg.id_or_slug, manager.db)
    if pkg:
        webbrowser.open(pkg.folders[0].path.as_uri())
    else:
        click.echo(MESSAGES['any_failure__not_installed'](id=addon[0]))


@main.group()
@click.pass_obj
def debug(manager):
    """Debugging funcionality."""


@debug.command(name='shell')
@click.pass_obj
def shell(manager):
    """Drop into an interactive shell.

    The shell is created in context and provides access to the
    currently-active manager.
    """
    try:
        from IPython import embed
    except ImportError:
        click.echo('ipython is not installed')
    else:
        embed()


@debug.group()
def cache():
    """Manage the resolver cache."""


@cache.command()
@click.pass_obj
def invalidate(manager):
    from datetime import datetime
    from .models import CacheEntry
    manager.db.query(CacheEntry).update({'date_retrieved': datetime.fromtimestamp(0)})
    manager.db.commit()


@cache.command()
@click.pass_obj
def clear(manager):
    from .models import CacheEntry
    manager.db.query(CacheEntry).delete()
    manager.db.commit()
