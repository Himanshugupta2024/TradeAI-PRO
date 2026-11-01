"""Extract screener-related code from server.py"""
lines = open('server.py', 'r', encoding='utf-8').readlines()

# Find _compute_gainers_losers_from_cache and screener endpoints
in_section = False
section_lines = []
for i, line in enumerate(lines):
    if '_compute_gainers_losers_from_cache' in line and 'def ' in line:
        in_section = True
        start = i
    if in_section:
        section_lines.append(f"{i+1:4d}: {line.rstrip()}")
        # Stop after we've captured enough (screener losers endpoint + some)
        if len(section_lines) > 120:
            break

print('\n'.join(section_lines))

# Also find sectors endpoint
print("\n\n=== SECTORS ENDPOINT ===")
for i, line in enumerate(lines):
    if 'def get_sectors' in line or '/api/sectors' in line:
        for j in range(max(0, i-1), min(len(lines), i+30)):
            print(f"{j+1:4d}: {lines[j].rstrip()}")
        print("---")
