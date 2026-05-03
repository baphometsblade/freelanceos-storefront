@echo off
echo ============================================
echo  FreelanceOS Email Capture Worker Deploy
echo ============================================
echo.
echo Step 1: Logging into Cloudflare...
echo (A browser window will open — click Allow)
echo.
cd /d "%~dp0"
call npx wrangler login
echo.
echo Step 2: Deploying worker...
call npx wrangler deploy
echo.
echo ============================================
echo  DONE! Copy the worker URL above and
echo  update WORKER_URL in index.html
echo ============================================
pause
