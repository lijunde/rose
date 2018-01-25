# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# (C) British Crown Copyright 2012-8 Met Office.
#
# This file is part of Rose, a framework for meteorological suites.
#
# Rose is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Rose is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Rose. If not, see <http://www.gnu.org/licenses/>.
# ----------------------------------------------------------------------------
"""Builtin application: rose_arch: transform and archive suite files."""

import errno
from glob import glob
import os
import re
from rose.app_run import (
    BuiltinApp,
    ConfigValueError,
    CompulsoryConfigValueError)
from rose.checksum import get_checksum, get_checksum_func
from rose.env import env_var_process, UnboundEnvironmentVariableError
from rose.popen import RosePopenError
from rose.reporter import Event
from rose.scheme_handler import SchemeHandlersManager
import shlex
import sqlite3
import sys
from tempfile import mkdtemp
from time import gmtime, strftime, time


class RoseArchDuplicateError(ConfigValueError):

    """An exception raised if dupluicate archive targets are provided."""

    ERROR_FORMAT = '%s: duplicate archive target%s: "%s"'


class RoseArchValueError(KeyError):

    """An error raised on a bad value."""

    ERROR_FORMAT = "%s: bad %s: %s: %s: %s"

    def __str__(self):
        return self.ERROR_FORMAT % self.args


class RoseArchEvent(Event):

    """Event raised on an archiving target."""

    def __str__(self):
        target = self.args[0]
        t_info = ""
        if len(self.args) > 1:
            times = self.args[1]
            t_init, t_tran, t_arch = times
            t_info = ", t(init)=%s, dt(tran)=%ds, dt(arch)=%ds" % (
                strftime("%Y-%m-%dT%H:%M:%SZ", gmtime(t_init)),
                t_tran - t_init,
                t_arch - t_tran
            )
        ret_code_str = ""
        if len(self.args) > 2 and self.args[2] is not None:
            ret_code_str = ", ret-code=%d" % self.args[2]
        ret = "%s %s [compress=%s%s%s]" % (
            target.status,
            target.name,
            target.compress_scheme,
            t_info,
            ret_code_str)
        if target.status != target.ST_OLD:
            for source in sorted(target.sources.values(),
                                 lambda s1, s2: cmp(s1.name, s2.name)):
                ret += "\n%s\t%s (%s)" % (
                    target.status, source.name, source.orig_name)
        return ret


class RoseArchApp(BuiltinApp):

    """Transform and archive files generated by suite tasks."""

    SCHEME = "rose_arch"
    SECTION = "arch"

    def run(self, app_runner, conf_tree, opts, args, uuid, work_files):
        """Transform and archive suite files.

        This application is designed to work under "rose task-run" in a suite.

        """
        dao = RoseArchDAO()
        suite_name = os.getenv("ROSE_SUITE_NAME")
        if not suite_name:
            return
        suite_dir = app_runner.suite_engine_proc.get_suite_dir(suite_name)
        cwd = os.getcwd()
        app_runner.fs_util.chdir(suite_dir)
        try:
            return self._run(dao, app_runner, conf_tree.node)
        finally:
            app_runner.fs_util.chdir(cwd)
            dao.close()

    def _run(self, dao, app_runner, config):
        """Transform and archive suite files.

        This application is designed to work under "rose task-run" in a suite.

        """
        compress_manager = SchemeHandlersManager(
            [os.path.dirname(os.path.dirname(sys.modules["rose"].__file__))],
            "rose.apps.rose_arch_compressions",
            ["compress_sources"],
            None, app_runner)

        # Set up the targets
        s_key_tails = set()
        targets = []
        for t_key, t_node in sorted(config.value.items()):
            if t_node.is_ignored() or ":" not in t_key:
                continue
            s_key_head, s_key_tail = t_key.split(":", 1)
            if s_key_head != self.SECTION or not s_key_tail:
                continue

            # Determine target path.
            s_key_tail = t_key.split(":", 1)[1]
            try:
                s_key_tail = env_var_process(s_key_tail)
            except UnboundEnvironmentVariableError as exc:
                raise ConfigValueError([t_key, ""], "", exc)

            # If parenthesised target is optional.
            is_compulsory_target = True
            if s_key_tail.startswith("(") and s_key_tail.endswith(")"):
                s_key_tail = s_key_tail[1:-1]
                is_compulsory_target = False

            # Don't permit duplicate targets.
            if s_key_tail in s_key_tails:
                raise RoseArchDuplicateError([t_key], '', s_key_tail)
            else:
                s_key_tails.add(s_key_tail)

            target = self._run_target_setup(
                app_runner, compress_manager, config, t_key, s_key_tail,
                t_node, is_compulsory_target)
            old_target = dao.select(target.name)
            if old_target is None or old_target != target:
                dao.delete(target)
            else:
                target.status = target.ST_OLD
            targets.append(target)
        targets.sort(key=lambda target: target.name)

        # Delete from database items that are no longer relevant
        dao.delete_all(filter_targets=targets)

        # Update the targets
        for target in targets:
            self._run_target_update(dao, app_runner, compress_manager, target)

        return [target.status for target in targets].count(
            RoseArchTarget.ST_BAD)

    def _run_target_setup(
            self, app_runner, compress_manager, config, t_key, s_key_tail,
            t_node, is_compulsory_target=True):
        """Helper for _run. Set up a target."""
        target_prefix = self._get_conf(
            config, t_node, "target-prefix", default="")
        target = RoseArchTarget(target_prefix + s_key_tail)
        target.command_format = self._get_conf(
            config, t_node, "command-format", compulsory=True)
        try:
            target.command_format % {"sources": "", "target": ""}
        except KeyError as exc:
            target.status = target.ST_BAD
            app_runner.handle_event(
                RoseArchValueError(
                    target.name,
                    "command-format",
                    target.command_format,
                    type(exc).__name__,
                    exc
                )
            )
        target.source_edit_format = self._get_conf(
            config, t_node, "source-edit-format", default="")
        try:
            target.source_edit_format % {"in": "", "out": ""}
        except KeyError as exc:
            target.status = target.ST_BAD
            app_runner.handle_event(
                RoseArchValueError(
                    target.name,
                    "source-edit-format",
                    target.source_edit_format,
                    type(exc).__name__,
                    exc
                )
            )
        update_check_str = self._get_conf(config, t_node, "update-check")
        try:
            checksum_func = get_checksum_func(update_check_str)
        except ValueError as exc:
            raise RoseArchValueError(
                target.name,
                "update-check",
                update_check_str,
                type(exc).__name__,
                exc)
        source_prefix = self._get_conf(
            config, t_node, "source-prefix", default="")
        for source_glob in shlex.split(
                self._get_conf(config, t_node, "source", compulsory=True)):
            is_compulsory_source = is_compulsory_target
            if source_glob.startswith("(") and source_glob.endswith(")"):
                source_glob = source_glob[1:-1]
                is_compulsory_source = False
            paths = glob(source_prefix + source_glob)
            if not paths:
                exc = OSError(errno.ENOENT, os.strerror(errno.ENOENT),
                              source_prefix + source_glob)
                app_runner.handle_event(ConfigValueError(
                    [t_key, "source"], source_glob, exc))
                if is_compulsory_source:
                    target.status = target.ST_BAD
                continue
            for path in paths:
                # N.B. source_prefix may not be a directory
                name = path[len(source_prefix):]
                for path_, checksum, _ in get_checksum(path, checksum_func):
                    if checksum is None:  # is directory
                        continue
                    if path_:
                        target.sources[checksum] = RoseArchSource(
                            checksum,
                            os.path.join(name, path_),
                            os.path.join(path, path_))
                    else:  # path is a file
                        target.sources[checksum] = RoseArchSource(
                            checksum, name, path)
        if not target.sources:
            if is_compulsory_target:
                target.status = target.ST_BAD
            else:
                target.status = target.ST_NULL
        target.compress_scheme = self._get_conf(config, t_node, "compress")
        if not target.compress_scheme:
            target_base = target.name
            if "/" in target.name:
                target_base = target.name.rsplit("/", 1)[1]
            if "." in target_base:
                tail = target_base.split(".", 1)[1]
                if compress_manager.get_handler(tail):
                    target.compress_scheme = tail
        elif compress_manager.get_handler(target.compress_scheme) is None:
            app_runner.handle_event(ConfigValueError(
                [t_key, "compress"],
                target.compress_scheme,
                KeyError(target.compress_scheme)))
            target.status = target.ST_BAD
        rename_format = self._get_conf(config, t_node, "rename-format")
        if rename_format:
            rename_parser_str = self._get_conf(config, t_node, "rename-parser")
            if rename_parser_str:
                try:
                    rename_parser = re.compile(rename_parser_str)
                except re.error as exc:
                    raise RoseArchValueError(
                        target.name,
                        "rename-parser",
                        rename_parser_str,
                        type(exc).__name__,
                        exc)
            else:
                rename_parser = None
            for source in target.sources.values():
                dict_ = {
                    "cycle": os.getenv("ROSE_TASK_CYCLE_TIME"),
                    "name": source.name}
                if rename_parser:
                    match = rename_parser.match(source.name)
                    if match:
                        dict_.update(match.groupdict())
                try:
                    source.name = rename_format % dict_
                except (KeyError, ValueError) as exc:
                    raise RoseArchValueError(
                        target.name,
                        "rename-format",
                        rename_format,
                        type(exc).__name__,
                        exc)
        return target

    @classmethod
    def _run_target_update(cls, dao, app_runner, compress_manager, target):
        """Helper for _run. Update a target."""
        if target.status == target.ST_OLD:
            app_runner.handle_event(RoseArchEvent(target))
            return
        if target.status in (target.ST_BAD, target.ST_NULL):
            # boolean to int
            target.command_rc = int(target.status == target.ST_BAD)
            if target.status == target.ST_BAD:
                level = Event.FAIL
            else:
                level = Event.DEFAULT
            event = RoseArchEvent(target)
            app_runner.handle_event(event)
            app_runner.handle_event(event, kind=Event.KIND_ERR, level=level)
            return
        target.command_rc = 1
        dao.insert(target)
        work_dir = mkdtemp()
        times = [time()] * 3  # init, transformed, archived
        ret_code = None
        try:
            # Rename/edit sources
            target.status = target.ST_BAD
            rename_required = False
            for source in target.sources.values():
                if source.name != source.orig_name:
                    rename_required = True
                    break
            if rename_required or target.source_edit_format:
                for source in target.sources.values():
                    source.path = os.path.join(work_dir, source.name)
                    app_runner.fs_util.makedirs(
                        os.path.dirname(source.path))
                    if target.source_edit_format:
                        command = target.source_edit_format % {
                            "in": source.orig_path,
                            "out": source.path}
                        app_runner.popen.run_ok(command, shell=True)
                    else:
                        app_runner.fs_util.symlink(source.orig_path,
                                                   source.path)
            # Compress sources
            if target.compress_scheme:
                handler = compress_manager.get_handler(
                    target.compress_scheme)
                handler.compress_sources(target, work_dir)
            times[1] = time()  # transformed time
            # Run archive command
            sources = []
            if target.work_source_path:
                sources = [target.work_source_path]
            else:
                for source in target.sources.values():
                    sources.append(source.path)
            command = target.command_format % {
                "sources": app_runner.popen.list_to_shell_str(sources),
                "target": app_runner.popen.list_to_shell_str([target.name])}
            ret_code, out, err = app_runner.popen.run(command, shell=True)
            times[2] = time()  # archived time
            if ret_code:
                app_runner.handle_event(
                    RosePopenError([command], ret_code, out, err))
            else:
                target.status = target.ST_NEW
                app_runner.handle_event(err, kind=Event.KIND_ERR)
            app_runner.handle_event(out)
            target.command_rc = ret_code
            dao.update_command_rc(target)
        finally:
            app_runner.fs_util.delete(work_dir)
            event = RoseArchEvent(target, times, ret_code)
            app_runner.handle_event(event)
            if target.status in (target.ST_BAD, target.ST_NULL):
                app_runner.handle_event(
                    event, kind=Event.KIND_ERR, level=Event.FAIL)

    def _get_conf(self, r_node, t_node, key, compulsory=False, default=None):
        """Return the value of a configuration."""
        value = t_node.get_value(
            [key],
            r_node.get_value([self.SECTION, key], default=default))
        if compulsory and not value:
            raise CompulsoryConfigValueError([key], None,
                                             KeyError(key))
        if value:
            try:
                value = env_var_process(value)
            except UnboundEnvironmentVariableError as exc:
                raise ConfigValueError([key], value, exc)
        return value


class RoseArchTarget(object):

    """An archive target."""

    ST_OLD = "="
    ST_NEW = "+"
    ST_BAD = "!"
    ST_NULL = "0"

    def __init__(self, name):
        self.name = name
        self.compress_scheme = None
        self.command_format = None
        self.command_rc = 0
        self.sources = {}  # checksum: RoseArchSource
        self.source_edit_format = None
        self.status = None
        self.work_source_path = None

    def __eq__(self, other):
        if id(self) != id(other):
            for key in ["name", "compress_scheme", "command_format",
                        "command_rc", "sources", "source_edit_format"]:
                if getattr(self, key) != getattr(other, key, None):
                    return False
        return True

    def __ne__(self, other):
        return not self.__eq__(other)


class RoseArchSource(object):

    """An archive source."""

    def __init__(self, checksum, orig_name, orig_path=None):
        self.checksum = checksum
        self.orig_name = orig_name
        self.orig_path = orig_path
        self.name = self.orig_name
        self.path = self.orig_path

    def __eq__(self, other):
        if id(self) != id(other):
            for key in ["checksum", "name"]:
                if getattr(self, key) != getattr(other, key, None):
                    return False
        return True

    def __ne__(self, other):
        return not self.__eq__(other)


class RoseArchDAO(object):

    """Data access object for incremental mode."""

    FILE_NAME = ".rose-arch.db"
    T_SOURCES = "sources"
    T_TARGETS = "targets"

    def __init__(self):
        self.file_name = os.path.abspath(self.FILE_NAME)
        self.conn = None
        self.create()

    def close(self):
        """Close connection to the SQLite database file."""
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def get_conn(self):
        """Connect to the SQLite database file."""
        if self.conn is None:
            self.conn = sqlite3.connect(self.file_name)
        return self.conn

    def create(self):
        """Create the database file if it does not exist."""
        if not os.path.exists(self.file_name):
            conn = self.get_conn()
            conn.execute("""CREATE TABLE """ + self.T_TARGETS + """ (
                            target_name TEXT,
                            compress_scheme TEXT,
                            command_format TEXT,
                            command_rc INT,
                            source_edit_format TEXT,
                            PRIMARY KEY(target_name))""")
            conn.execute("""CREATE TABLE """ + self.T_SOURCES + """ (
                            target_name TEXT,
                            source_name TEXT,
                            checksum TEXT,
                            UNIQUE(target_name, checksum))""")
            conn.commit()

    def delete(self, target):
        """Remove target from the database."""
        conn = self.get_conn()
        for name in [self.T_TARGETS, self.T_SOURCES]:
            conn.execute("DELETE FROM " + name + " WHERE target_name==?",
                         [target.name])
        conn.commit()

    def delete_all(self, filter_targets):
        """Remove all but those matching filter_targets from the database."""
        conn = self.get_conn()
        where = ""
        stmt_args = []
        if filter_targets:
            stmt_fragments = []
            for filter_target in filter_targets:
                stmt_fragments.append("target_name != ?")
                stmt_args.append(filter_target.name)
            where += " WHERE " + " AND ".join(stmt_fragments)
        for name in [self.T_TARGETS, self.T_SOURCES]:
            conn.execute("DELETE FROM " + name + where, stmt_args)
        conn.commit()

    def insert(self, target):
        """Insert a target in the database."""
        conn = self.get_conn()
        t_stmt = "INSERT INTO " + self.T_TARGETS + " VALUES (?, ?, ?, ?, ?)"
        t_stmt_args = [target.name, target.compress_scheme,
                       target.command_format, target.command_rc,
                       target.source_edit_format]
        conn.execute(t_stmt, t_stmt_args)
        sh_stmt = r"INSERT INTO " + self.T_SOURCES + " VALUES (?, ?, ?)"
        sh_stmt_args = [target.name]
        for checksum, source in target.sources.items():
            conn.execute(sh_stmt, sh_stmt_args + [source.name, checksum])
        conn.commit()

    def select(self, target_name):
        """Query database for target_name.

        On success, reconstruct the target as an instance of RoseArchTarget
        and return it.

        Return None on failure.

        """
        conn = self.get_conn()
        t_stmt = (
            "SELECT " +
            "compress_scheme,command_format,command_rc,source_edit_format " +
            "FROM " +
            self.T_TARGETS +
            " WHERE target_name==?"
        )
        t_stmt_args = [target_name]
        for row in conn.execute(t_stmt, t_stmt_args):
            target = RoseArchTarget(target_name)
            (target.compress_scheme,
                target.command_format,
                target.command_rc,
                target.source_edit_format) = row
            break
        else:
            return None
        s_stmt = ("SELECT source_name,checksum FROM " + self.T_SOURCES +
                  " WHERE target_name==?")
        s_stmt_args = [target_name]
        for s_row in conn.execute(s_stmt, s_stmt_args):
            source_name, checksum = s_row
            target.sources[checksum] = RoseArchSource(checksum, source_name)
        return target

    def update_command_rc(self, target):
        """Update the command return code of a target in the database."""
        conn = self.get_conn()
        conn.execute("UPDATE " + self.T_TARGETS + " SET command_rc=?" +
                     " WHERE target_name==?", [target.command_rc, target.name])
        conn.commit()
