#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "=========================================="
echo "CPU Limits Research - Phase 2 Analysis"
echo "=========================================="
echo

# Check for evidence files
evidence_count=$(find evidence/ -name "*.json" -type f ! -name "summary.json" | wc -l)
echo "Found $evidence_count evidence files"
echo

if [ "$evidence_count" -eq 0 ]; then
  echo "❌ No evidence files found. Run experiments first:"
  echo "   python3 run-experiments.py"
  exit 1
fi

# Run throttling analysis
echo "1. Analyzing throttling patterns..."
python3 analyze-throttling.py > analysis-output.txt 2>&1
echo "   ✓ analysis-output.txt"
echo

# Generate dashboard
echo "2. Generating interactive dashboard..."
python3 generate-dashboard.py > dashboard-generation.log 2>&1
echo "   ✓ dashboard.html"
echo

# Count experiments by QoS class
echo "3. Summary by QoS class:"
for qos in besteffort burstable guaranteed; do
  count=$(find evidence/ -name "*-$qos-*.json" -type f | wc -l)
  echo "   - $qos: $count experiments"
done
echo

echo "=========================================="
echo "Analysis complete!"
echo "=========================================="
echo
echo "Next steps:"
echo "  • View analysis:    cat analysis-output.txt"
echo "  • View dashboard:   firefox dashboard.html"
echo "  • Read findings:    cat README.md"
echo

# Show snippet of analysis
echo "Preview of analysis (first 30 lines):"
echo "--------------------------------------"
head -30 analysis-output.txt
