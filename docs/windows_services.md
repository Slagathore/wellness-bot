# Running Services on Windows

For development or deployment on Windows, use Task Scheduler (or dedicated service wrappers such as NSSM) to keep each worker running.

## One-Time Setup
1. Open `Task Scheduler` ➜ `Create Basic Task`.
2. Name it (e.g., `WellnessBot_Runtime`).
3. Trigger: `When the computer starts` (or `log on`).
4. Action: `Start a Program` with:
   - **Program/script**: `powershell`
   - **Add arguments**:
     ```
     -NoProfile -ExecutionPolicy Bypass -Command "cd '<repo-path>'; python -m venv .venv; .\.venv\Scripts\Activate.ps1; python -m app.main_modular"
     ```
   - Adjust paths for your environment.
5. Finish and set `Run whether user is logged on or not`.

Repeat for each worker (replace the module at the end):

| Task Name | Command |
|-----------|---------|
| `WellnessBot_Runtime` | `python -m app.main_modular` |
| `WellnessBot_Admin` | `python -m app.interfaces.admin.server` |
| `WellnessBot_Outbox` | `python -m app.workers.outbox_sender` |
| `WellnessBot_Embeddings` | `python -m app.workers.embeddings` |
| `WellnessBot_Sentiments` | `python -m app.workers.sentiments` |
| `WellnessBot_Nightly` | `python -m app.workers.nightly` |

## Quick PowerShell Launch (Manual)
To run a worker manually, open PowerShell in the repo and execute:
```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
python -m app.main_modular
```
Launch each worker in its own terminal.

## Logs
PowerShell produces stdout/stderr directly. For persistent logging, redirect output:
```powershell
python -m app.main_modular *> logs\runtime.log
```
