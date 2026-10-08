#!/bin/sh
# One-time setup: point git at the version-controlled hooks directory.
git config core.hooksPath hooks
echo "Git hooks configured: core.hooksPath set to 'hooks'."
