# Classifies every CodeQL result in a SARIF file as BLOCK or REPORT.
#
# Used by .github/workflows/codeql.yml when findings cannot be uploaded to the Security tab and the
# workflow therefore has to make the pass/fail decision itself. Kept as a file rather than inlined
# in the workflow so it can be run against a downloaded SARIF by hand:
#
#   jq -r --arg threshold 7.0 -f .github/codeql-gate.jq python.sarif
#
# Emits one TSV row per result: verdict, level, security-severity, rule, location, message.
#
# Severity is resolved by rule id out of `tool.extensions[].rules`, which is the only place it
# exists: CodeQL puts no `level` on individual results, and `tool.driver.rules` is empty in the
# SARIF this workflow produces. Reading `.level` off a result instead silently treats every finding
# as a warning, which is how a gate ends up either blocking on unused imports or blocking on
# nothing.
[ .runs[].tool.extensions[]?.rules[]? ] as $rules
| ( reduce $rules[] as $r ({};
      .[$r.id] = {
        lvl: ($r.defaultConfiguration.level // "warning"),
        sec: (($r.properties["security-severity"] // "") | tostring)
      }
  ) ) as $meta
| .runs[].results[]?
| . as $res
# A rule absent from the metadata is treated as an unscored warning rather than assumed harmless.
| ($meta[$res.ruleId] // { lvl: "warning", sec: "" }) as $m
| ( if ($m.sec != "" and (($m.sec | tonumber) >= ($threshold | tonumber)))
    then "BLOCK" else "REPORT" end ) as $verdict
| [
    $verdict,
    $m.lvl,
    (if $m.sec == "" then "-" else $m.sec end),
    $res.ruleId,
    ( (($res.locations[0].physicalLocation.artifactLocation.uri) // "?")
      + ":"
      + ((($res.locations[0].physicalLocation.region.startLine) // 0) | tostring) ),
    # Tabs and newlines would corrupt the TSV the caller parses with cut(1).
    ($res.message.text | gsub("[\t\n]"; " "))
  ]
| @tsv
