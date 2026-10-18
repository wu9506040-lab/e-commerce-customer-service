#!/bin/bash
export PATH=/home/zw/.local/bin:/usr/bin:/usr/local/bin:$PATH
echo "===try install chromium==="
timeout 60 /home/zw/.local/bin/playwright install chromium 2>&1
echo "EXIT=$?"
echo "===cache==="
ls -la /home/zw/.cache/ms-playwright/ 2>&1
