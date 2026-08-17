param(
    [int]$ApiPort = 8000,
    [int]$DashboardPort = 8501
)

$workspaceRoot = (Resolve-Path -LiteralPath "$PSScriptRoot\..").Path
$python = Join-Path $workspaceRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment is missing. Run 'uv sync' from $workspaceRoot first."
}

$api = Start-Process -FilePath $python -ArgumentList @("-m", "uvicorn", "marketsentinel.api.app:app", "--host", "127.0.0.1", "--port", "$ApiPort") -WorkingDirectory $workspaceRoot -WindowStyle Hidden -PassThru

try {
    & $python -m streamlit run "src\marketsentinel\dashboard.py" "--server.port=$DashboardPort" "--browser.gatherUsageStats=false"
} finally {
    if (-not $api.HasExited) {
        Stop-Process -Id $api.Id
    }
}

