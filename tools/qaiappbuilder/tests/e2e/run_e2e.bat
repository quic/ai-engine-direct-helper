@echo off
rem ---------------------------------------------------------------------
rem Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
rem SPDX-License-Identifier: BSD-3-Clause
rem ---------------------------------------------------------------------
rem Launch the E2E suite by feature category and write a report.
rem   run_e2e.bat --list
rem   run_e2e.bat -c chat,security
rem   run_e2e.bat -c settings -- -k proxy
rem See "run_e2e.bat --help" and the docstring of run_e2e.py.
rem
rem Python used to RUN the launcher (stdlib only), first match wins:
rem   %QAI_E2E_TEST_PYTHON%, %TEMP%\qai-e2e-venv, py -3, python
setlocal
set "HERE=%~dp0"
set "PY="
if defined QAI_E2E_TEST_PYTHON if exist "%QAI_E2E_TEST_PYTHON%" set "PY=%QAI_E2E_TEST_PYTHON%"
if not defined PY if exist "%TEMP%\qai-e2e-venv\Scripts\python.exe" set "PY=%TEMP%\qai-e2e-venv\Scripts\python.exe"
if not defined PY (where py >nul 2>nul && set "PY=py -3")
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo [run_e2e] No Python found. Set QAI_E2E_TEST_PYTHON to a Python that has pytest installed. 1>&2
  exit /b 2
)
%PY% "%HERE%run_e2e.py" %*
exit /b %ERRORLEVEL%
