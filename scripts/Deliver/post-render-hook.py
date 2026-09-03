#!/usr/bin/env python3
# Post-render hook.
#
# When a render job finishes, Resolve makes its JobId available as the global
# 'job'. We look that job up in the render job list, build the rendered file
# path (TargetDir/OutputFilename), and run the bash trigger with it as $1.
#
# The bash trigger (post_render.sh) and the log live in the .local Scripts
# folder, located by search so it works no matter which copy of this .py runs.

import os
import sys
import datetime
import subprocess
import inspect
import pathlib

SCRIPT = inspect.getfile(inspect.currentframe())
SCRIPT_HOME = pathlib.Path(SCRIPT).parent.resolve()

LOG = os.path.join(SCRIPT_HOME, "post_render.log")


def log(msg):
    line = "%s  %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


def get_resolve():
    return app.GetResolve()


def find_trigger():
    cands = [
        os.path.join(SCRIPT_HOME, "post_render.sh"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def run_trigger(trigger, path):
    if not trigger:
        log("no post_render.sh found in the .local Scripts folder (or set POST_RENDER_SH)")
        return
    log("trigger: %s" % trigger)
    log("running trigger on: %s" % path)
    try:
        os.spawnv(os.P_WAIT, trigger, [trigger, path, os.path.dirname(path)])
        result = subprocess.run( [trigger, path])
    except Exception as e:
        log("trigger failed: %r" % e)


def main():
    app = get_resolve()
    if not app:
        log("no Resolve API this run")
        return

    trigger = find_trigger()
    job_id = globals().get("job")
    pm = app.GetProjectManager()
    project = pm.GetCurrentProject()
    if not project:
        log("no current project")
        return
    jobs = project.GetRenderJobList() or []

    job = next((j for j in jobs if j.get("JobId") == job_id), None)
    if not job:
        log("job %r not found; available JobIds: %r" % (job_id, [j.get("JobId") for j in jobs]))
        return

    rendered_file = os.path.join(job.get("TargetDir") or "", job.get("OutputFilename") or "")
    log("rendered file: %s" % rendered_file)
    run_trigger(trigger, rendered_file)


if __name__ == "__main__":
    main()
