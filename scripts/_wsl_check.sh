#!/bin/bash
export PATH=/home/zw/.local/bin:$PATH
echo "===VERSION==="
goofish --version 2>&1 | head -3
echo "===AUTH STATUS==="
timeout 10 goofish auth status --format json 2>&1 | head -10
echo "===HOME==="
echo "$HOME"
ls -la "$HOME/.goofish-cli" 2>&1 | head -8
