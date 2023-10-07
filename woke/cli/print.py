import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    Any,
    Dict,
    FrozenSet,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    Union,
)

import rich_click as click

if TYPE_CHECKING:
    from woke.printers import Printer


logger = logging.getLogger(__name__)


class PrintCli(click.RichGroup):  # pyright: ignore reportPrivateImportUsage
    _plugin_commands: Dict[str, click.Command] = {}
    _failed_plugin_paths: Set[Tuple[Path, Exception]] = set()
    _failed_plugin_entry_points: Set[Tuple[str, Exception]] = set()
    _printer_collisions: Set[Tuple[str, str, str]] = set()
    _completion_mode: bool
    _global_data_path: Path
    _loading_from_plugins: bool = False
    loaded_from_plugins: Dict[str, Union[str, Path]] = {}
    _current_plugin: Union[str, Path] = ""
    _plugins_loaded: bool = False

    def __init__(
        self,
        name: Optional[str] = None,
        commands: Optional[
            Union[Dict[str, click.Command], Sequence[click.Command]]
        ] = None,
        **attrs: Any,
    ):
        super().__init__(name=name, commands=commands, **attrs)

        import os
        import platform

        self._completion_mode = "_WOKE_COMPLETE" in os.environ

        system = platform.system()
        try:
            self._global_data_path = Path(os.environ["XDG_DATA_HOME"]) / "woke"
        except KeyError:
            if system in {"Linux", "Darwin"}:
                self._global_data_path = Path.home() / ".local" / "share" / "woke"
            elif system == "Windows":
                self._global_data_path = Path(os.environ["LOCALAPPDATA"]) / "woke"
            else:
                raise RuntimeError(f"Unsupported system: {system}")

        for command in self.commands.values():
            self._inject_params(command)

    @staticmethod
    def _inject_params(command: click.Command) -> None:
        for param in command.params:
            if isinstance(param, click.Option):
                param.show_default = True
                param.show_envvar = True

        command.params.append(
            click.Argument(
                ["paths"],
                nargs=-1,
                type=click.Path(exists=True),
            )
        )

    @property
    def failed_plugin_paths(self) -> FrozenSet[Tuple[Path, Exception]]:
        return frozenset(self._failed_plugin_paths)

    @property
    def failed_plugin_entry_points(self) -> FrozenSet[Tuple[str, Exception]]:
        return frozenset(self._failed_plugin_entry_points)

    @property
    def printer_collisions(self) -> FrozenSet[Tuple[str, str, str]]:
        return frozenset(self._printer_collisions)

    def add_verified_plugin_path(self, path: Path) -> None:
        import json

        try:
            with open(self._global_data_path.joinpath("verified-printers.json")) as f:
                data = {Path(d) for d in json.load(f)}
        except FileNotFoundError:
            data = set()

        data.add(path)
        with open(self._global_data_path.joinpath("verified-printers.json"), "w") as f:
            json.dump([str(p) for p in data], f)

    def _verify_plugin_path(self, path: Path) -> bool:
        import json

        from rich.prompt import Confirm

        if path == self._global_data_path / "global-printers":
            return True

        try:
            with open(self._global_data_path.joinpath("verified-printers.json")) as f:
                data = {Path(d) for d in json.load(f)}
        except FileNotFoundError:
            data = set()
        if path not in data:
            if self._completion_mode:
                return False

            verified = Confirm.ask(f"Do you trust printers in {path}?", default=False)
            if verified:
                data.add(path)
                with open(
                    self._global_data_path.joinpath("verified-printers.json"), "w"
                ) as f:
                    json.dump([str(p) for p in data], f)
            return verified
        return True

    def _load_plugins(
        self, plugin_paths: AbstractSet[Path], verify_paths: bool
    ) -> None:
        if sys.version_info < (3, 10):
            from importlib_metadata import entry_points
        else:
            from importlib.metadata import entry_points
        from importlib.util import module_from_spec, spec_from_file_location

        self._loading_from_plugins = True
        for cmd in self.loaded_from_plugins.keys():
            self.commands.pop(cmd, None)
        self.loaded_from_plugins.clear()
        self._failed_plugin_paths.clear()
        self._failed_plugin_entry_points.clear()
        self._printer_collisions.clear()

        printer_entry_points = entry_points().select(group="woke.plugins.printers")
        for entry_point in sorted(printer_entry_points, key=lambda e: e.value):
            self._current_plugin = entry_point.value

            # unload target module and all its children
            for m in [
                k
                for k in sys.modules.keys()
                if k == entry_point.value or k.startswith(entry_point.value + ".")
            ]:
                sys.modules.pop(m)

            try:
                entry_point.load()
            except Exception as e:
                self._failed_plugin_entry_points.add((entry_point.value, e))
                if not self._completion_mode:
                    logger.error(
                        f"Failed to load printers from package '{entry_point.value}': {e}"
                    )

        for path in [self._global_data_path / "global-printers"] + sorted(plugin_paths):
            if not path.exists() or (
                verify_paths and not self._verify_plugin_path(path)
            ):
                continue
            self._current_plugin = path
            sys.path.insert(0, str(path.parent))
            try:
                # unload target module and all its children
                for m in [
                    k
                    for k in sys.modules.keys()
                    if k == path.stem or k.startswith(path.stem + ".")
                ]:
                    sys.modules.pop(m)

                if path.is_dir():
                    spec = spec_from_file_location(path.stem, str(path / "__init__.py"))
                else:
                    spec = spec_from_file_location(path.stem, str(path))

                if spec is not None and spec.loader is not None:
                    module = module_from_spec(spec)
                    spec.loader.exec_module(module)
                else:
                    raise RuntimeError(f"spec_from_file_location returned None")
            except Exception as e:
                self._failed_plugin_paths.add((path, e))
                sys.path.pop(0)
                if not self._completion_mode:
                    logger.error(f"Failed to load printers from path {path}: {e}")

        self._loading_from_plugins = False

    def add_command(self, cmd: click.Command, name: Optional[str] = None) -> None:
        name = name or cmd.name
        if name in self.loaded_from_plugins:
            if isinstance(self.loaded_from_plugins[name], str):
                prev = f"package '{self.loaded_from_plugins[name]}'"
            else:
                prev = f"path '{self.loaded_from_plugins[name]}'"
            if isinstance(self._current_plugin, str):
                current = f"package '{self._current_plugin}'"
            else:
                current = f"path '{self._current_plugin}'"

            self._printer_collisions.add((name, prev, current))
            if not self._completion_mode:
                logger.warning(
                    f"Printer '{name}' loaded from {current} overrides printer loaded from {prev}"
                )

        self._inject_params(cmd)
        super().add_command(cmd, name)
        if self._loading_from_plugins:
            self.loaded_from_plugins[
                name
            ] = self._current_plugin  # pyright: ignore reportGeneralTypeIssues

    def get_command(
        self,
        ctx: click.Context,
        cmd_name: str,
        plugin_paths: AbstractSet[Path] = frozenset([Path.cwd() / "printers"]),
        force_load_plugins: bool = False,
        verify_paths: bool = True,
    ) -> Optional[click.Command]:
        if not self._plugins_loaded or force_load_plugins:
            self._load_plugins(plugin_paths, verify_paths)
            self._plugins_loaded = True
        return self.commands.get(cmd_name)

    def list_commands(
        self,
        ctx: click.Context,
        plugin_paths: AbstractSet[Path] = frozenset([Path.cwd() / "printers"]),
        force_load_plugins: bool = False,
        verify_paths: bool = True,
    ) -> List[str]:
        if not self._plugins_loaded or force_load_plugins:
            self._load_plugins(plugin_paths, verify_paths)
            self._plugins_loaded = True
        return sorted(self.commands)

    def invoke(self, ctx: click.Context):
        ctx.obj["subcommand_args"] = ctx.args
        ctx.obj["subcommand_protected_args"] = ctx.protected_args
        super().invoke(ctx)


@click.group(
    name="print", cls=PrintCli, context_settings={"auto_envvar_prefix": "WOKE_PRINTER"}
)
@click.option(
    "--no-artifacts", is_flag=True, default=False, help="Do not write build artifacts."
)
@click.pass_context
def run_print(ctx: click.Context, no_artifacts: bool) -> None:
    """Run a printer."""

    if "--help" in ctx.obj["subcommand_args"]:
        return

    from ..compiler import SolcOutputSelectionEnum, SolidityCompiler
    from ..compiler.build_data_model import ProjectBuild
    from ..compiler.solc_frontend import SolcOutputError, SolcOutputErrorSeverityEnum
    from ..config import WokeConfig
    from ..utils import get_class_that_defined_method
    from ..utils.file_utils import is_relative_to
    from .console import console

    config = WokeConfig()
    config.load_configs()  # load ~/.woke/config.toml and ./woke.toml

    sol_files: Set[Path] = set()
    start = time.perf_counter()
    with console.status("[bold green]Searching for *.sol files...[/]"):
        for file in config.project_root_path.rglob("**/*.sol"):
            if (
                not any(
                    is_relative_to(file, p) for p in config.compiler.solc.ignore_paths
                )
                and file.is_file()
            ):
                sol_files.add(file)
    end = time.perf_counter()
    console.log(
        f"[green]Found {len(sol_files)} *.sol files in [bold green]{end - start:.2f} s[/bold green][/]"
    )

    compiler = SolidityCompiler(config)
    compiler.load(console=console)

    build: ProjectBuild
    errors: Set[SolcOutputError]
    build, errors = asyncio.run(
        compiler.compile(
            sol_files,
            [SolcOutputSelectionEnum.ALL],
            write_artifacts=not no_artifacts,
            console=console,
            no_warnings=True,
        )
    )

    errored = any(
        error.severity == SolcOutputErrorSeverityEnum.ERROR for error in errors
    )
    if errored:
        sys.exit(1)

    assert compiler.latest_build_info is not None
    assert compiler.latest_graph is not None

    assert isinstance(ctx.command, PrintCli)
    assert ctx.invoked_subcommand is not None
    command = ctx.command.get_command(ctx, ctx.invoked_subcommand)
    assert command is not None
    assert command.name is not None

    if hasattr(config.printers, command.name):
        default_map = getattr(config.printers, command.name)
    else:
        default_map = None

    cls: Type[Printer] = get_class_that_defined_method(
        command.callback
    )  # pyright: ignore reportGeneralTypeIssues
    if cls is not None:

        def _callback(*args, **kwargs):
            instance.paths = [Path(p).resolve() for p in kwargs.pop("paths", [])]

            original_callback(
                instance, *args, **kwargs
            )  # pyright: ignore reportOptionalCall

        instance = cls()
        instance.build = build
        instance.build_info = compiler.latest_build_info
        instance.config = config
        instance.console = console
        instance.imports_graph = (  # pyright: ignore reportGeneralTypeIssues
            compiler.latest_graph.copy()
        )
        instance.logger = logging.getLogger(cls.__name__)
        if ctx.obj["debug"]:
            instance.logger.setLevel(logging.DEBUG)

        original_callback = command.callback
        command.callback = _callback

        sub_ctx = command.make_context(
            command.name,
            [*ctx.obj["subcommand_protected_args"][1:], *ctx.obj["subcommand_args"]],
            parent=ctx,
            default_map=default_map,
        )
        with sub_ctx:
            sub_ctx.command.invoke(sub_ctx)

        instance._run()
    else:

        def _callback(*args, **kwargs):
            click.get_current_context().obj["paths"] = [
                Path(p).resolve() for p in kwargs.pop("paths", [])
            ]

            original_callback(*args, **kwargs)  # pyright: ignore reportOptionalCall

        assert command.callback is not None

        args = [*ctx.obj["subcommand_protected_args"][1:], *ctx.obj["subcommand_args"]]
        logger = logging.getLogger(command.callback.__name__)
        if ctx.obj["debug"]:
            logger.setLevel(logging.DEBUG)

        ctx.obj = {
            "build": build,
            "build_info": compiler.latest_build_info,
            "config": config,
            "console": console,
            "imports_graph": compiler.latest_graph.copy(),
            "logger": logger,
        }

        original_callback = command.callback
        command.callback = _callback

        sub_ctx = command.make_context(
            command.name, args, parent=ctx, default_map=default_map
        )
        with sub_ctx:
            sub_ctx.command.invoke(sub_ctx)

    # avoid double execution of a subcommand
    sys.exit(0)