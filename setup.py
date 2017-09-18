
from setuptools import setup

from instawow import __version__


setup(name='instawow',
      version=__version__,
      url='http://github.com/layday/instawow',
      author='layday',
      author_email='layday@protonmail.com',
      classifiers=['Development Status :: 3 - Alpha',
                   'License :: OSI Approved :: GNU General Public License v3 (GPLv3)',
                   'Programming Language :: Python :: 3.6'],
      description='Add-on manager for World of Warcraft.',
      packages=['instawow'],
      install_requires=['aiohttp>=2.2.5,<3', 'click>=6.7,<7',
                        'lxml>=3.8.0,<4', 'pydantic>=0.4,<1',
                        'SQLAlchemy>=1.1.13,<2', 'texttable>=0.9.1,<1',
                        'uvloop>=0.8.0,<1', 'yarl>=0.12.0,<1'],
      extras_require={'test': ['pytest>=3.2.1,<4', 'vcrpy>=1.11.1,<2']},
      dependency_links=['git+https://bitbucket.org/astanin/python-tabulate@feature/multiline-rows#egg=tabulate-0.8.0',
                        'git+https://github.com/kevin1024/vcrpy@c3ecf8c#egg=vcrpy-1.11.1'],
      entry_points={'console_scripts': ['instawow = instawow.cli:cli',
                                        'instawow-init = instawow.cli:init']})
