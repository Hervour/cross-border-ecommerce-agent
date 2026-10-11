$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
python -m uvicorn order_fulfillment:app --host 127.0.0.1 --port 8000 --reload