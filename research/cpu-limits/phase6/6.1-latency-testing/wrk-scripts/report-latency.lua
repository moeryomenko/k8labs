-- wrk Lua script for detailed latency reporting
-- Reports P50, P75, P90, P95, P99, P99.9, P99.99

done = function(summary, latency, requests)
  io.write("================================================================================\n")
  io.write("Latency Distribution (detailed)\n")
  io.write("================================================================================\n")
  io.write("\n")
  
  -- Percentiles to report
  local percentiles = {50, 75, 90, 95, 99, 99.9, 99.99}
  
  for _, p in ipairs(percentiles) do
    local n = latency:percentile(p)
    io.write(string.format("  P%-6.2f: %8.3fms\n", p, n / 1000))
  end
  
  io.write("\n")
  io.write("================================================================================\n")
  io.write("Throughput\n")
  io.write("================================================================================\n")
  io.write("\n")
  io.write(string.format("  Requests/sec: %.2f\n", summary.requests / summary.duration * 1e6))
  io.write(string.format("  Transfer/sec: %.2fMB\n", summary.bytes / summary.duration * 1e6 / 1024 / 1024))
  io.write("\n")
end
