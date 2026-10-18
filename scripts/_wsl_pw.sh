#!/bin/bash
set -e
export PATH=/home/zw/.local/bin:/usr/bin:/usr/local/bin:$PATH
echo "===playwright==="
~/.local/bin/playwright --version 2>&1
echo "===playwright install chrome==="
~/.local/bin/playwright install chrome 2>&1 | tail -15
echo "===cache==="
ls ~/.cache/ms-playwright/ 2>&1 | head -5
