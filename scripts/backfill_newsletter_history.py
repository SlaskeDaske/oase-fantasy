name: Genskab tidligere nyhedsbreve (engangs)

on:
  workflow_dispatch: {}

jobs:
  backfill:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Genskab historik
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          PYTHONUNBUFFERED: '1'
        run: python scripts/backfill_newsletter_history.py
      - name: Commit hvis der er ændringer
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          if [ -n "$(git status --porcelain newsletter.json)" ]; then
            git add newsletter.json
            git commit -m "Genskab tidligere nyhedsbreve (engangs-backfill)"
            git push
          fi
