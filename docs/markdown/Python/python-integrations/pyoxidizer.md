---
title: "PyOxidizer"
slug: "pyoxidizer"
excerpt: "Creating Python binaries through PyOxidizer."
hidden: false
createdAt: "2022-02-04T18:41:48.950Z"
updatedAt: "2022-02-28T23:26:51.526Z"
---
PyOxidizer allows you to distribute your code as a single binary file, similar to [Pex files](doc:pex-files). Unlike Pex, these binaries include a Python interpreter, often greatly simplifying distribution. 

See our blog post on [Packaging Python with the Pants PyOxidizer Plugin](https://blog.pantsbuild.org/packaging-python-with-the-pyoxidizer-pants-plugin/) for more discussion of the benefits of PyOxidizer.

Step 1: Activate the backend
----------------------------

Add this to your `pants.toml`:

```toml pants.toml
[GLOBAL]
backend_packages.add = [
  "pants.backend.experimental.python.packaging.pyoxidizer",
  "pants.backend.python",
]
```

This adds the new `pyoxidizer_binary` target, which you can confirm by running `./pants help pyoxidizer_binary`.

> 🚧 This backend is experimental
> 
> We are still discovering the best ways to provide PyOxidizer support, such as how to make our [default template more useful](https://github.com/pantsbuild/pants/pull/14183/files#r788253973). This backend does not follow the normal [deprecation policy](doc:deprecation-policy), although we will do our best to minimize breaking changes.
> 
> We would [love your feedback](doc:getting-help) on this backend!

Step 2: Define a `python_distribution` target
---------------------------------------------

The `pyoxidizer_binary` target works by pointing to a `python_distribution` target with the code you want included. Pants then passes the distribution to PyOxidizer to install it as a binary. 

So, to get started, create a `python_distribution` target per [Building distributions](doc:python-distributions). 

```python project/BUILD
python_sources(name="lib")

python_distribution(
    name="dist",
    dependencies=[":lib"],
    provides=python_artifact(name="my-dist", version="0.0.1"),
)
```

The `python_distribution` must produce at least one wheel (`.whl`) file. If you are using Pants's default of `generate_setup=True`, make sure you also use Pants's default of `wheel=True`. Pants will eagerly error when building your `pyoxidizer_binary` if you use a `python_distribution` that does not produce wheels.

Step 3: Define a `pyoxidizer_binary` target
-------------------------------------------

Now, create a `pyoxidizer_binary` target and set the `dependencies` field to the [address](doc:targets) of the `python_distribution` you created previously.

```python project/BUILD
pyoxidizer_binary(
    name="bin",
    dependencies=[":dist"],
)
```

Usually, you will want to set the `entry_point` field, which sets the behavior for what happens when you run the binary. 

If the `entry_point` field is not specified, running the binary will launch a Python interpreter with all the relevant code and dependencies loaded.

```bash
❯ ./dist/bin/x86_64-apple-darwin/release/install/bin
Python 3.9.7 (default, Oct 18 2021, 00:59:13) 
[Clang 13.0.0 ] on darwin
Type "help", "copyright", "credits" or "license" for more information.
>>> from myproject import myapp
>>> myapp.main()
Hello, world!
>>>
```

You can instead set `entry_point` to the Python module to execute (e.g. `myproject.myapp`). If specified, running the binary will launch the application similar to if it had been run as `python -m myproject.myapp`, for example.

```python
pyoxidizer_binary(
    name="bin",
    dependencies=[":dist"],
    entry_point="myproject.myapp",
)
```

```bash
❯ ./dist/bin/x86_64-apple-darwin/release/install/bin
Launching myproject.myapp from __main__
Hello, world!
```

Step 4: Run `package` or `run` goals
------------------------------------

Finally, run `./pants package $address` on your `pyoxidizer_binary` target to create a directory
including your binary, or `./pants run $address` to launch the binary.

For example:

```
❯ ./pants package src/py/project:bin
14:15:31.18 [INFO] Completed: Building src.py.project:bin with PyOxidizer
14:15:31.23 [INFO] Wrote dist/src.py.project/bin/aarch64-apple-darwin/debug/install/bin
```
```
❯ ./pants run src/py/project:bin
14:15:31.18 [INFO] Completed: Building src.py.project:bin with PyOxidizer
Hello, world!
```

By default, with the `package` goal, Pants will write the package using this scheme: `dist/{path.to.tgt_dir}/{tgt_name}/{platform}/{debug,release}/install/{tgt_name}`. You can change the first part of this path by setting the `output_path` field, although you risk name collisions with other `pyoxidizer_binary` targets in your project. See [pyoxidizer_binary](doc:reference-pyoxidizer_binary) for more info.

> 🚧 `debug` vs `release` builds
> 
> By default, PyOxidizer will build with Rust's "debug" mode, which results in much faster compile times but means that your binary will be slower to run. Instead, you can instruct PyOxidizer to build in [release mode](https://nnethercote.github.io/perf-book/build-configuration.html#release-builds) by adding this to `pants.toml`:
> 
> ```toml
> [pyoxidizer]
> args = ["--release"]
> ```
> 
> Or by using the command line flag `./pants --pyoxidizer-args='--release' package path/to:tgt`.

Advanced use cases
------------------

> 👍 Missing functionality? Let us know!
> 
> We would like to keep improving Pants's PyOxidizer support. We encourage you to let us know what features are missing through [Slack or GitHub](doc:getting-help)!

> 🚧 `[python-repos]` not yet supported for custom indexes
> 
> Currently, PyOxidizer can only resolve dependencies from PyPI and your first-party code. If you need support for custom indexes, please let us know by commenting on <https://github.com/pantsbuild/pants/issues/14619>. 
> 
> (We'd be happy to help mentor someone through this change, although please still comment either way!)

### `python_distribution`s that implicitly depend on each other

As explained at [Building distributions](doc:python-distributions#mapping-source-files-to-distributions), Pants automatically detects when one `python_distribution` depends on another, and it will add that dependency to the `install_requires` for the distribution. 

When this happens, PyOxidizer would naively try installing that first-party dependency from PyPI, which will likely fail. Instead, include all relevant `python_distribution` targets in the `dependencies` field of the `pyoxidizer_binary` target.

```python project/BUILD
python_sources(name="lib")

python_distribution(
    name="dist",
    # Note that this python_distribution does not 
    # explicitly include project/utils:dist in its
    # `dependencies` field, but Pants still 
    # detects an implicit dependency and will add 
    # it to this dist's `install_requires`.
    dependencies=[":lib"],
    provides=setup_py(name="main-dist", version="0.0.1"),
)

pyoxidizer_binary(
    name="bin",
    entry_point="hellotest.main",
    dependencies=[":dist", "project/utils:dist"],
)
```
```python project/main.py
from hellotest.utils.greeter import GREET

print(GREET)
```
```python project/utils/greeter.py
GREET = 'Hello world!'
```
```python project/utils/BUILD
python_sources(name="lib")

python_distribution(
    name="dist",
    dependencies=[":lib"],
    provides=setup_py(name="utils-dist", version="0.0.1"),
)
```

### `template` field

If the default PyOxidizer configuration that Pants generates is too limiting, a custom template can be used instead. Pants will expect a file with the extension `.bzlt` in a path relative to the `BUILD` file. 

```python
pyoxidizer_binary(
    name="bin",
    dependencies=[":dist"],
    entry_point="myproject.myapp",
    template="pyoxidizer.bzlt",
)
```

The custom `.bzlt` may use four parameters from within the Pants build process inside the template (these parameters must be prefixed by `$` or surrounded with `${ }` in the template). 

- `RUN_MODULE` - The re-formatted `entry_point` passed to this target (or None).
- `NAME` - This target's name.
- `WHEELS` - All python distributions passed to this target (or `[]`).
- `UNCLASSIFIED_RESOURCE_INSTALLATION` - This will populate a snippet of code to correctly inject the target's `filesystem_resources`.

For example, in a custom PyOxidizer configuration template, to use the `pyoxidizer_binary` target's `name` field:

```python
exe = dist.to_python_executable(
    name="$NAME",
    packaging_policy=policy,
    config=python_config,
)
```

You almost certainly will want to include this line, which is how the `dependencies` field gets consumed:

```python
exe.add_python_resources(exe.pip_install($WHEELS))
```

### `filesystem_resources` field

As explained in [PyOxidizer's documentation](https://pyoxidizer.readthedocs.io/en/stable/pyoxidizer_packaging_additional_files.html#installing-unclassified-files-on-the-filesystem), you may sometimes need to force certain dependencies to be installed to the filesystem. You can do that with the `filesystem_resources` field:

```python
pyoxidizer_binary(
    name="bin",
    dependencies=[":dist"],
    entry_point="myproject.myapp",
    filesystem_resources=["numpy==1.17"],
)
```
