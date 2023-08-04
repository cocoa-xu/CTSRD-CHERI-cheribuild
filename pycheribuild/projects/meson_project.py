#
# Copyright (c) 2016 Alex Richardson
# All rights reserved.
#
# This software was developed by SRI International and the University of
# Cambridge Computer Laboratory under DARPA/AFRL contract FA8750-10-C-0237
# ("CTSRD"), as part of the DARPA CRASH research programme.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
#
import contextlib
import itertools
import os
import shutil
from pathlib import Path
from typing import Sequence

from .project import MakeCommandKind, _CMakeAndMesonSharedLogic
from ..config.chericonfig import BuildType
from ..config.target_info import BasicCompilationTargets, NativeTargetInfo
from ..utils import InstallInstructions, OSInfo, include_local_file, remove_duplicates

__all__ = ["MesonProject"]  # no-combine


class MesonProject(_CMakeAndMesonSharedLogic):
    do_not_add_to_targets: bool = True
    make_kind: MakeCommandKind = MakeCommandKind.Ninja
    compile_db_requires_bear: bool = False  # generated by default
    default_build_type: BuildType = BuildType.RELWITHDEBINFO
    generate_cmakelists: bool = False  # Can use compilation DB
    # Meson already sets PKG_CONFIG_* variables internally based on the cross toolchain
    set_pkg_config_path: bool = False
    _configure_tool_name: str = "Meson"
    meson_test_script_extra_args: "Sequence[str]" = tuple()  # additional arguments to pass to run_meson_tests.py
    _meson_extra_binaries = ""  # Needed for picolibc
    _meson_extra_properties = ""  # Needed for picolibc

    def set_minimum_meson_version(self, major: int, minor: int, patch: int = 0) -> None:
        new_version = (major, minor, patch)
        assert self._minimum_cmake_or_meson_version is None or new_version >= self._minimum_cmake_or_meson_version
        self._minimum_cmake_or_meson_version = new_version

    def _configure_tool_install_instructions(self) -> InstallInstructions:
        return OSInfo.install_instructions(
            "meson", False, default="meson", homebrew="meson", zypper="meson", freebsd="meson", apt="meson",
            alternative="run `pip3 install --upgrade --user meson` to install the latest version")

    @classmethod
    def setup_config_options(cls, **kwargs) -> None:
        super().setup_config_options(**kwargs)
        cls.meson_options = cls.add_list_option("meson-options", metavar="OPTIONS",
                                                help="Additional command line options to pass to Meson")

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.configure_command = os.getenv("MESON_COMMAND", None)
        if self.configure_command is None:
            self.configure_command = "meson"
        self.configure_args.insert(0, "setup")
        # We generate a toolchain file when cross-compiling and the toolchain files need at least 0.57
        self.set_minimum_meson_version(0, 57)

    @property
    def _native_toolchain_file(self) -> Path:
        assert not self.compiling_for_host()
        return self.build_dir / "meson-native-file.ini"

    def add_meson_options(self, _include_empty_vars=False, _replace=True, **kwargs) -> None:
        return self._add_configure_options(_config_file_options=self.meson_options, _replace=_replace,
                                           _include_empty_vars=_include_empty_vars, **kwargs)

    def setup(self) -> None:
        super().setup()
        self._toolchain_template = include_local_file("files/meson-machine-file.ini.in")
        if not self.compiling_for_host():
            assert self.target_info.is_freebsd() or self.target_info.is_baremetal(), "Only tested FreeBSD/baremetal"
            self._toolchain_file = self.build_dir / "meson-cross-file.ini"
            self.configure_args.extend(["--cross-file", str(self._toolchain_file)])
            # We also have to pass a native machine file to override pkg-config/cmake search dirs for host tools
            self.configure_args.extend(["--native-file", str(self._native_toolchain_file)])
        else:
            # Recommended way to override compiler is using a native config file:
            self._toolchain_file = self.build_dir / "meson-native-file.ini"
            self.configure_args.extend(["--native-file", str(self._toolchain_file)])
            # PKG_CONFIG_LIBDIR can only be set in the toolchain file when cross-compiling, set it in the environment
            # for CheriBSD with pkg-config installed via pkg64.
            if self.target_info.pkg_config_libdir_override is not None:
                self.configure_environment.update(PKG_CONFIG_LIBDIR=self.target_info.pkg_config_libdir_override)
                self.make_args.set_env(PKG_CONFIG_LIBDIR=self.target_info.pkg_config_libdir_override)
        if self.force_configure and not self.with_clean and (self.build_dir / "meson-info").exists():
            self.configure_args.append("--reconfigure")
        # Don't use bundled fallback dependencies, we always want to use the (potentially patched) system packages.
        self.configure_args.append("--wrap-mode=nofallback")
        self.add_meson_options(**self.build_type.to_meson_args())
        if self.use_lto:
            self.add_meson_options(b_lto=True, b_lto_threads=self.config.make_jobs,
                                   b_lto_mode="thin" if self.get_compiler_info(self.CC).is_clang else "default")
        if self.use_asan:
            self.add_meson_options(b_sanitize="address,undefined", b_lundef=False)

        # Unlike CMake, Meson does not set the DT_RUNPATH entry automatically:
        # See https://github.com/mesonbuild/meson/issues/6220, https://github.com/mesonbuild/meson/issues/6541, etc.
        if not self.compiling_for_host():
            extra_libdirs = [s / self.target_info.default_libdir for s in self.dependency_install_prefixes]
            with contextlib.suppress(LookupError):  # If there isn't a rootfs, we use the absolute paths instead.
                # If we are installing into a rootfs, remove the rootfs prefix from the RPATH
                extra_libdirs = ["/" + str(s.relative_to(self.rootfs_dir)) for s in extra_libdirs]
            rpath_dirs = remove_duplicates(self.target_info.additional_rpath_directories + extra_libdirs)
            if rpath_dirs:
                self.COMMON_LDFLAGS.append("-Wl,-rpath=" + ":".join(map(str, rpath_dirs)))

    def needs_configure(self) -> bool:
        return not (self.build_dir / "build.ninja").exists()

    def _toolchain_file_list_to_str(self, values: list) -> str:
        # The meson toolchain file uses python-style lists
        assert all(isinstance(x, str) or isinstance(x, Path) for x in values), \
            "All values should be strings/Paths: " + str(values)
        return str(list(map(str, values)))

    def _bool_to_str(self, value: bool) -> str:
        return "true" if value else "false"

    @property
    def _get_version_args(self) -> dict:
        return dict(regex=b"(\\d+)\\.(\\d+)\\.?(\\d+)?")

    def configure(self, **kwargs) -> None:
        pkg_config_bin = shutil.which("pkg-config") or "pkg-config"
        cmake_bin = shutil.which(os.getenv("CMAKE_COMMAND", "cmake")) or "cmake"
        self._prepare_toolchain_file_common(
            self._toolchain_file,
            TOOLCHAIN_LINKER=self.target_info.linker,
            TOOLCHAIN_MESON_CPU_FAMILY=self.crosscompile_target.cpu_architecture.as_meson_cpu_family(),
            TOOLCHAIN_ENDIANESS=self.crosscompile_target.cpu_architecture.endianess(),
            MESON_EXTRA_BINARIES=self._meson_extra_binaries,
            MESON_EXTRA_PROPERTIES=self._meson_extra_properties,
            TOOLCHAIN_PKGCONFIG_BINARY=pkg_config_bin,
            TOOLCHAIN_CMAKE_BINARY=cmake_bin,
        )
        if not self.compiling_for_host():
            native_toolchain_template = include_local_file("files/meson-cross-file-native-env.ini.in")
            # Create a stub NativeTargetInfo to obtain the host {CMAKE_PREFIX,PKG_CONFIG}_PATH.
            # NB: we pass None as the project argument here to ensure the results do not different between projects.
            # noinspection PyTypeChecker
            host_target_info = NativeTargetInfo(BasicCompilationTargets.NATIVE, None)  # pytype: disable=wrong-arg-types
            host_prefixes = self.host_dependency_prefixes
            assert self.config.other_tools_dir in host_prefixes
            host_pkg_config_dirs = list(itertools.chain.from_iterable(
                host_target_info.pkgconfig_candidates(x) for x in host_prefixes))
            self._replace_values_in_toolchain_file(
                native_toolchain_template, self._native_toolchain_file,
                NATIVE_C_COMPILER=self.host_CC, NATIVE_CXX_COMPILER=self.host_CXX,
                TOOLCHAIN_PKGCONFIG_BINARY=pkg_config_bin, TOOLCHAIN_CMAKE_BINARY=cmake_bin,
                # To find native packages we have to add the bootstrap tools to PKG_CONFIG_PATH and CMAKE_PREFIX_PATH.
                NATIVE_PKG_CONFIG_PATH=remove_duplicates(host_pkg_config_dirs),
                NATIVE_CMAKE_PREFIX_PATH=remove_duplicates(
                    host_prefixes + host_target_info.cmake_prefix_paths(self.config)),
            )

        if self.install_prefix != self.install_dir:
            assert self.destdir, "custom install prefix requires DESTDIR being set!"
            self.add_meson_options(prefix=self.install_prefix)
        else:
            self.add_meson_options(prefix=self.install_dir)
        # Meson setup --reconfigure does not update cached dependencies, we have to manually run
        # `meson configure --clearcache` (https://github.com/mesonbuild/meson/issues/6180).
        if self.force_configure and not self.with_clean and (self.build_dir / "meson-info").exists():
            self.configure_args.append("--reconfigure")
            self.run_cmd(self.configure_command, "configure", "--clearcache", cwd=self.build_dir)
        self.configure_args.append(str(self.source_dir))
        self.configure_args.append(str(self.build_dir))
        super().configure(**kwargs)
        if self.config.copy_compilation_db_to_source_dir and (self.build_dir / "compile_commands.json").exists():
            self.install_file(self.build_dir / "compile_commands.json", self.source_dir / "compile_commands.json",
                              force=True)

    def run_tests(self) -> None:
        if self.compiling_for_host():
            self.run_cmd(self.configure_command, "test", "--print-errorlogs", cwd=self.build_dir)
        elif self.target_info.is_cheribsd():
            self.target_info.run_cheribsd_test_script("run_meson_tests.py", *self.meson_test_script_extra_args,
                                                      mount_builddir=True, mount_sysroot=True, mount_sourcedir=True,
                                                      use_full_disk_image=self.tests_need_full_disk_image)
        else:
            self.info("Don't know how to run tests for", self.target, "when cross-compiling for",
                      self.crosscompile_target)
