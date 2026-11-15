#include <Python.h>
#ifndef LIGHT_ROOT
#error LIGHT_ROOT is required
#endif
int main(int argc, char **argv) {
    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.parse_argv = 0;
    config.use_environment = 0;
    PyStatus status = PyConfig_SetBytesString(&config, &config.program_name,
                                             LIGHT_ROOT "/.venv/bin/python");
    if (PyStatus_Exception(status)) Py_ExitStatusException(status);
    status = PyConfig_SetBytesString(&config, &config.run_filename, LIGHT_ROOT "/light.py");
    if (PyStatus_Exception(status)) Py_ExitStatusException(status);
    argv[0] = LIGHT_ROOT "/light.py";
    status = PyConfig_SetBytesArgv(&config, argc, argv);
    if (PyStatus_Exception(status)) Py_ExitStatusException(status);
    status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) Py_ExitStatusException(status);
    return Py_RunMain();
}
