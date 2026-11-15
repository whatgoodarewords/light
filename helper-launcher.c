/*
 * Light.app: the helper that owns this tool's Bluetooth permission.
 *
 * macOS grants Bluetooth to an app. This small program is that app: its
 * Info.plist carries NSBluetoothAlwaysUsageDescription, it starts agent.py
 * with the tool's own Python, and it stays alive until agent.py ends, so the
 * permission prompt and the grant belong to "Light" whichever terminal or
 * script ran the `light` command.
 *
 * Layout (the folder that contains Light.app):
 *   Light.app/Contents/MacOS/Light   this program
 *   .venv/bin/python                 Python with bleak and cryptography
 *   agent.py                         the helper
 */
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static pid_t child = 0;

static void forward(int sig) {
  if (child > 0) kill(child, sig);
}

int main(int argc, char **argv) {
  (void)argc;
  (void)argv;
  char exe[PATH_MAX], real[PATH_MAX], root[PATH_MAX];
  uint32_t size = sizeof(exe);
  if (_NSGetExecutablePath(exe, &size) != 0 || realpath(exe, real) == NULL) {
    fprintf(stderr, "Light: cannot find my own path\n");
    return 70;
  }
  char *p = real; /* <root>/Light.app/Contents/MacOS/Light -> <root> */
  for (int i = 0; i < 4; i++) p = dirname(p);
  strlcpy(root, p, sizeof(root));

  char python[PATH_MAX], script[PATH_MAX];
  snprintf(python, sizeof(python), "%s/.venv/bin/python", root);
  snprintf(script, sizeof(script), "%s/agent.py", root);
  char *args[] = {python, "-u", script, NULL};

  signal(SIGINT, forward);
  signal(SIGTERM, forward);
  signal(SIGHUP, forward);

  int err = posix_spawn(&child, python, NULL, NULL, args, environ);
  if (err != 0) {
    fprintf(stderr, "Light: cannot start %s: %s\n", python, strerror(err));
    return 71;
  }
  int status = 0;
  while (waitpid(child, &status, 0) < 0) {
    /* interrupted by a forwarded signal: wait again */
  }
  if (WIFEXITED(status)) return WEXITSTATUS(status);
  return 128 + (WIFSIGNALED(status) ? WTERMSIG(status) : 0);
}
