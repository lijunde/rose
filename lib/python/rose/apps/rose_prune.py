# -*- coding: utf-8 -*-
#-----------------------------------------------------------------------------
# (C) British Crown Copyright 2012-3 Met Office.
#
# This file is part of Rose, a framework for scientific suites.
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
#-----------------------------------------------------------------------------
"""Builtin application: rose_prune: suite housekeeping application."""

from glob import glob
import os
from rose.app_run import BuiltinApp
from rose.date import RoseDateShifter, OffsetValueError
from rose.fs_util import FileSystemEvent
from rose.popen import RosePopenError
from rose.suite_log_view import SuiteLogViewGenerator
import shlex

class RosePruneApp(BuiltinApp):

    """Prune files and directories generated by suite tasks."""

    SCHEME = "rose_prune"
    SECTION = "prune"

    def run(self, app_runner, config, opts, args, uuid, work_files):
        """Suite housekeeping application.

        This application is designed to work under "rose task-run" in a cycling
        suite.

        """
        suite_name = os.getenv("ROSE_SUITE_NAME")
        if not suite_name:
            return
        cycles = shlex.split(config.get_value([self.SECTION, "cycles"]))
        config_globs_str = config.get_value([self.SECTION, "globs"])
        if not cycles and not config_globs_str:
            return
        ds = RoseDateShifter(task_cycle_time_mode=True)
        for cycle in cycles:
            if ds.is_task_cycle_time_mode() and ds.is_offset(cycle):
                cycle = ds.date_shift(offset=cycle)
        if cycles:
            slvg = SuiteLogViewGenerator(
                    event_handler=app_runner.event_handler,
                    fs_util=app_runner.fs_util,
                    popen=app_runner.popen,
                    suite_engine_proc=app_runner.suite_engine_proc)
            slvg.generate(suite_name, cycles, tidy_remote_mode=True,
                          archive_mode=True)
        suite_engine_proc = app_runner.suite_engine_proc
        globs = []
        for cycle in cycles:
            globs.extend(suite_engine_proc.get_cycle_items_globs(cycle))
        globs += shlex.split(config.get_value([self.SECTION, "globs"], ""))
        hosts = suite_engine_proc.get_suite_jobs_auths(suite_name)
        suite_dir_rel = suite_engine_proc.get_suite_dir_rel(suite_name)
        sh_cmd_args = {"d": suite_dir_rel, "g": " ".join(globs)}
        sh_cmd = "cd %(d)s && ls -d %(g)s && rm -rf %(g)" % sh_cmd_args
        for host in hosts:
            cmd = app_runner.popen.get_cmd("ssh", host, sh_cmd)
            try:
                out, err = app_runner.popen.run_ok(*cmd)
            except RosePopenError as e:
                app_runner.handle_event(e)
            else:
                for line in out.splitlines():
                    name = host + ":" + suite_dir_rel + "/" + line
                    event = FileSystemEvent(FileSystemEvent.DELETE, name)
                    app_runner.handle_event(event)
        cwd = os.getcwd()
        app_runner.chdir(app_runner.get_suite_dir(suite_name))
        try:
            for g in globs:
                for name in glob(g):
                    app_runner.fs_util.delete(name)
        finally:
            app_runner.chdir(cwd)
        return
