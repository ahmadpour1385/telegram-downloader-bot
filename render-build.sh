#!/bin/bash

set -e

pip install -r requirements.txt

curl -fsSL https://deno.land/install.sh | sh

export PATH="$HOME/.deno/bin:$PATH"

echo "=== DENO CHECK ==="
which deno
deno --version
