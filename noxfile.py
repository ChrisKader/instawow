from __future__ import annotations

import nox


@nox.session(reuse_venv=True)
def reformat(session: nox.Session):
    "Reformat Python source code using Black and JavaScript using Prettier."
    session.install('isort >=5.8.0', 'black >=20.8b1')
    for cmd in ('isort', 'black'):
        session.run(cmd, 'instawow', 'tests', 'noxfile.py', 'setup.py')

    if '--skip-prettier' not in session.posargs:
        session.chdir('gui')
        session.run(
            'npx',
            'prettier',
            '--write',
            '../pyrightconfig.json',
            'src',
            'package.json',
            'rollup.config.js',
            'tsconfig.json',
            external=True,
        )


@nox.session(python=['3.7', '3.8', '3.9'])
@nox.parametrize(
    'constraints',
    [
        '',
        '''\
aiohttp           ==3.7.4
alembic           ==1.4.3
click             ==7.1
jellyfish         ==0.8.2
jinja2            ==2.11.0
loguru            ==0.1.0
pluggy            ==0.13.0
prompt-toolkit    ==3.0.15
pydantic          ==1.8.0
questionary       ==1.8.0
sqlalchemy        ==1.3.19
typing-extensions ==3.7.4.3
yarl              ==1.4
''',
    ],
    [
        'none',
        'minimum-versions',
    ],
)
def test(session: nox.Session, constraints: str):
    "Run the test suite."
    tmp_dir = session.create_tmp()
    session.run('git', 'clone', '.', tmp_dir)
    session.chdir(tmp_dir)

    constraints_txt = 'constraints.txt'
    with open(constraints_txt, 'w') as file:
        file.write(constraints)

    session.install(
        '-c',
        constraints_txt,
        './.[server, test]',
        './tests/plugin',
    )
    session.run('coverage', 'run', '-m', 'pytest')
    session.run('coverage', 'report', '-m')


@nox.session(python=['3.7', '3.8', '3.9'])
def type_check(session: nox.Session):
    "Run Pyright."
    # The instawow path is hardcoded in pyrightconfig.json relative
    # to the enclosing folder, therefore we can't install instawow in a
    # virtual environment or Pyright won't be able to find it.
    # The next best (least worst) thing is to copy the repo into a
    # temporary folder before performing an editable install so that we don't
    # end up polluting the working directory.  An editable install would not
    # have been required at all if it weren't for ``_version.py`` which is
    # generated by setuptools_scm at build time and is imported in ``utils.py``.
    tmp_dir = session.create_tmp()
    session.run('git', 'clone', '.', tmp_dir)
    session.chdir(tmp_dir)
    session.install(
        '-e',
        '.[server]',
        'sqlalchemy-stubs@ https://github.com/layday/sqlalchemy-stubs/archive/develop.zip',
    )
    session.run('npx', 'pyright@1.1.129')


@nox.session(python=False)
def clobber_build_artefacts(session: nox.Session):
    "Remove build artefacts left behind by setuptools."
    session.run('rm', '-rf', 'build', 'dist', 'instawow/_version.py', 'instawow.egg-info')


@nox.session
def build(session: nox.Session):
    "Build instawow sdist and wheel."
    clobber_build_artefacts(session)
    session.install('build >=0.1.0')
    session.run('python', '-m', 'build', '.')


@nox.session
def publish(session: nox.Session):
    "Validate and upload distributions to PyPI."
    session.install('twine')
    for subcmd in ('check', 'upload'):
        session.run('twine', subcmd, 'dist/*')
