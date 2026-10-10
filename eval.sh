#!/usr/bin/env bash
# conflict-eval: does a model take the side of whoever is talking in a
# two-person conflict, and does it cave when pushed?
#
#   ./eval.sh check            check the scenarios against their own rules
#   ./eval.sh plan             count the new model calls a run would make, and estimate the cost
#   ./eval.sh run --dry-run    render every request body to results/dry-run/, send nothing
#   ./eval.sh run --yes        run the conversations (a cached call costs nothing)
#   ./eval.sh judge --yes      score every reply with both LLM judges
#   ./eval.sh report           the two numbers per model, and the worst transcripts
#   ./eval.sh sheet            export the blind human scoring sheet
#   ./eval.sh agree            kappa from the two filled sheets, and against each judge
#   ./eval.sh kit build        render the paste kit page for the app arm
#   ./eval.sh kit import PATH  turn the kit's saved replies into transcripts
#   ./eval.sh test             run the unit tests
#
# plan, run, and judge take --target NAME and --scenario ID; plan and run
# take --side A|B; plan and judge take --judge NAME.
# Keys come from the environment: ANTHROPIC_API_KEY, GEMINI_API_KEY,
# OPENAI_API_KEY. A target whose key is missing is skipped.
set -euo pipefail

case "${1:-}" in
  -h|--help|\?|"") awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; exit 0 ;;
esac

cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

if [ "$1" = test ]; then
  shift
  exec .venv/bin/python -m unittest discover -s tests "$@"
fi
exec .venv/bin/python -m conflict_eval "$@"
