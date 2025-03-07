---
title: "Building distributions"
slug: "python-distributions"
excerpt: "Packaging your code into an sdist or a wheel."
hidden: false
createdAt: "2020-03-16T16:19:55.626Z"
updatedAt: "2022-05-10T00:44:24.595Z"
---
A standard packaging format for Python code is the _distribution_: an archive that is published to a package index such as [PyPI](https://pypi.org/), and can be installed by [pip](https://packaging.python.org/key_projects/#pip). The two standard distribution archive types are [sdists](https://packaging.python.org/overview/#python-source-distributions) and [wheels](https://packaging.python.org/overview/#python-binary-distributions).

This page explains how to use Pants to build distributions from your code.

> 👍 Benefit of Pants: multiple distributions from a single repository
> 
> Typically, repositories without sophisticated tooling end up building a single distribution which includes the entire repo. But Pants makes it easy to create multiple distributions from the same repository.

Background: setuptools and PEP 517
----------------------------------

For a long time, [Setuptools](https://setuptools.pypa.io/) was the de-facto standard mechanism for building Python distributions.  Setuptools relies on a `setup.py` script that you provide in your code. This script contains the instructions on what code to package into the distribution and what the requirements and other metadata of the distribution should be. 

In the past few years, however, a new standard for specifying distribution builds has emerged: [PEP 517](https://www.python.org/dev/peps/pep-0517/). Under this standard (and its companion standard, [PEP 518](https://www.python.org/dev/peps/pep-0518/)) you use `pyproject.toml` to specify the python requirements and entry point for the builder code. This information is referred to as a _build backend_.  

Examples of build backends include Setuptools, but also other systems with package-building capabilities, such as [Flit](https://flit.readthedocs.io/en/latest/) or [Poetry](https://github.com/python-poetry/poetry-core).

Pants reads a PEP 517 `[build-system]` specification from `pyproject.toml` and applies it to build your distributions. That is, Pants acts as a _build frontend_ in PEP 517 parlance.  It is common to continue to use Setuptools as the build backend, but doing so via PEP 517 lets you control the exact version of Setuptools that gets used, as well as any other requirements that must be present at build time.

If there is no `pyproject.toml` with a `[build-system]` table available, Pants falls back to using Setuptools directly. 

The `python_distribution` target
--------------------------------

You configure a distribution using a [`python_distribution`](doc:reference-python_distribution) target. This target provides Pants with the information needed to build the distribution. 

### PEP 517

If using a PEP 517 `pyproject.toml` file, you might have a target layout similar to this:

```python example/dists/BUILD
resource(name="pyproject", source="pyproject.toml")

python_distribution(
    name="mydist",
    dependencies=[
        ":pyproject",
        # Dependencies on code to be packaged into the distribution.
    ],
    provides=python_artifact(
        name="mydist",
        version="2.21.0",
    ),
    # Example of setuptools config, other build backends may have other config.
    wheel_config_settings={"--global-option": ["--python-tag", "py37.py38.py39"]},
    # Don't use setuptools with a generated setup.py. 
    # You can also turn this off globally in pants.toml:
    #
    # [setup-py-generation]
    # generate_setup_default = false
    generate_setup = False,
)
```

Running `./pants package example/dists:mydist` will cause Pants to inspect the `[build-system]` table in `pyproject.toml`, install the requirements specified in that table's `requires` key, and then execute the entry point specified in the `build-backend` key to build an sdist and a wheel, just as PEP 517 requires.

If you want to build just a wheel or just an sdist, you can set `sdist=False` or `wheel=False` on the `python_distribution` target.

### Setuptools

If relying on legacy Setuptools behavior, you don't have a `pyproject.toml` resource, so your target is simply:

```python example/dists/BUILD
python_distribution(
    name="mydist",
    dependencies=[
        # Dependencies on code to be packaged into the distribution.
    ],
    provides=python_artifact(
        name="mydist",
        version="2.21.0",
    ),
    wheel_config_settings={"--global-option": ["--python-tag", "py37.py38.py39"]},
)
```

Running `./pants package example/dists:mydist` will cause Pants to run Setuptools, which will in turn run the `setup.py` script in the `python_distribution` target's directory. If no such script exists, Pants can generate one for you (see below).

> 📘 See `package` for other package formats
> 
> This page focuses on building sdists and wheels with the `./pants package` goal. See [package](doc:python-package-goal) for information on other formats that can be built with `./pants package`, such as PEX binaries and zip/tar archives.

setup.py
--------

Although alternatives exist, and PEP 517 enables them, Setuptools is still by far the most common choice for building distributions, whether via PEP 517 config, or directly via legacy support. If using Setuptools in either fashion, you need a `setup.py` script alongside your `python_distribution` target (and the target needs to depend on that script, typically via an explicit dependency on a `python_sources` target that owns it).

You can either author `setup.py` yourself (which is necessary if building native extensions), or have Pants generate one for you (see below).

By default Pants will generate a `setup.py` for every `python_distribution` target, unless you set `generate_setup = False` on the target. But you can flip this behavior by setting `generate_setup_default = false` in the `[setup-py-generation]` section of your `pants.toml` config file. In that case Pants will only generate a `setup.py` for `python_distribution` targets that have `generate_setup = True` set on them.

So if you expect to use handwritten `setup.py` scripts for most distributions in your repo, you probably want to set `generate-setup-default = false` and override it as needed. If you expect to mostly use generated `setup.py` scripts, you can set `generate-setup-default = true` (or just not set it, since that is the default).

Using a generated `setup.py`
----------------------------

Much of the data you would normally put in a `setup.py` file is already known to Pants, so it can be convenient to let Pants generate `setup.py` files for you, instead of maintaining them manually for each distributable project. 

In this case, you may want to add some information to the `provides= ` field in the `python_distribution` target, for Pants to place in the generated `setup.py`:

```python example/dists/BUILD
python_distribution(
    name="mydist",
    dependencies=[
        # Dependencies on code to be packaged into the distribution.
    ],
	provides=python_artifact(
        name="mydist",
        version="2.21.0",
        description="An example distribution built with Pants.",
        author="Pantsbuild",
        classifiers=[
            "Programming Language :: Python :: 3.7",
        ],
    ),
    wheel_config_settings={"--global-option": ["--python-tag", "py37.py38.py39"]},
)
```

Some important `setup.py` metadata is inferred by Pants from your code and its dependencies. Other metadata needs to be provided explicitly.  In Pants, as shown above, you do so through the `provides` field. 

You can use almost any [keyword argument](https://packaging.python.org/guides/distributing-packages-using-setuptools/#setup-args) accepted by `setup.py` in the `setup()` function. 

However, you cannot use `data_files`, `install_requires`, `namespace_packages`, `package_dir`, `package_data`, or `packages` because Pants will generate these for you, based on the data derived from your code and dependencies.

> 📘 Use the `entry_points` field to register entry points like `console_scripts`
> 
> The [`entry_points` field](doc:reference-python_distribution#codeentry_pointscode) allows you to configure [setuptools-style entry points](https://packaging.python.org/specifications/entry-points/#entry-points-specification):
> 
> ```python
> python_distribution(
>    name="my-dist",
>    entry_points={
>        "console_scripts": {"some-command": "project.app:main"},
>        "flake8_entry_point": {
>            "PB1": "my_flake8_plugin:Plugin",
>            "PB2": "my_flake8_plugin:AnotherPlugin",
>        },
>    provides=python_artifact(...),
> )
> ```
> 
> Pants will infer dependencies on each entry point, which you can confirm by running `./pants dependencies path/to:python_dist`.
> 
> In addition to using the format `path.to.module:func`, you can use an [address](doc:targets) to a `pex_binary` target, like `src/py/project:pex_binary` or `:sibling_pex_binary`. Pants will use the `entry_point` already specified by the `pex_binary`, and it will infer a dependency on the `pex_binary` target. This allows you to better DRY your project's entry points.

> 📘 Consider writing a plugin to dynamically generate the `setup()` keyword arguments
> 
> You may want to write a plugin to do any of these things:
> 
> - Reduce boilerplate by hardcoding common arguments and commands.
> - Read from the file system to dynamically determine kwargs, such as the `long_description` or `version`.
> - Run processes like Git to dynamically determine the `version` kwarg.
> 
> Start by reading about the [Plugin API](doc:plugins-overview), then refer to the [Custom `python_artifact()` kwargs](doc:plugins-setup-py) instructions.

Mapping source files to distributions
-------------------------------------

A Pants repo typically consists of one `python_source` target per file (usually generated by several `python_sources` targets). To build multiple distributions from the same repo, Pants must determine which libraries are bundled into each distribution. 

In the extreme case, you could have one distribution per `python_source` target, but publishing and consuming a distribution per file would of course not be practical. So in practice, multiple source files are bundled into a single distribution. 

Naively, you might think that a `python_distribution` publishes all the code of all the `python_source` targets it transitively depends on. But that could easily lead to trouble if you have multiple distributions that share common dependencies. You typically don't want the same code published in multiple distributions, as this can lead to all sorts of runtime import issues.

If you use a handwritten `setup.py`, you have to figure this out for yourself - Pants will bundle whatever the script tells it to.  But if you let Pants generate `setup.py` then it will apply the following algorithm:

Given a `python_distribution` target D, take all the source files in the transitive dependency closure of D. Some of those source files may be published in D itself, but others may be published in some other `python_distribution` target, D', in which case Pants will correctly add a requirement on D' in the metadata for D.

For each `python_source` target S, the distribution in which S's code is published is chosen to be:

1. A `python_distribution` that depends, directly or indirectly, on S.
2. Is S's closest filesystem ancestor among those satisfying 1.

If there are multiple such exported source files at the same degree of ancestry, the ownership
is ambiguous and an error is raised. If there is no `python_distribution` that depends on S
and is its ancestor, then there is no owner and an error is raised.

This algorithm implies that all source files published by a distribution must be below it in the filesystem. It also guarantees that a source file is only published by a single distribution.

The generated `setup.py` will have its `install_requires` set to include the 3rdparty dependencies of the code bundled in the distribution, plus any other distributions from your own repo. For example, if distribution D1 contains code that has a dependency on some source file S, and that source file is published in distribution D2, then D1's requirements will include a dependency on D2.  In other words, Pants does the right thing.

> 📘 Changing the versioning scheme for first-party dependencies
> 
> When a `python_distribution` depends on another `python_distribution`, Pants will add it to the `install_requires` value in the generated `setup.py`. 
> 
> By default, Pants will use exact requirements for first-party dependencies, like `other_dist==1.0.1`. You can set `first_party_depenency_version_scheme` in the `[setup-py-generation]` scope to `'compatible'` to use `~=` instead of `==`, and `any` to leave off the version.
> 
> For example:
> 
> ```toml
> [setup-py-generation]
> first_party_depenency_version_scheme = "compatible"
> ```
> 
> See <https://www.python.org/dev/peps/pep-0440/#version-specifiers> for more information on the `~=` specifier.

> 📘 How to publish your distributions to a package index
> 
> See [publish](doc:python-publish-goal) for example support publishing distributions using Twine.
