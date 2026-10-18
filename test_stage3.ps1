param([int]$Port = 8001)

# Stage 3 smoke tests: itinerary persistence, error shape, CORS, restart.
# Self-contained: starts its own uvicorn on port 8001, tests, then stops it.
# Port 8000 is never touched. Run with:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\test_stage3.ps1
# Covers: add (201), duplicate (409), missing/blank/wrong-type fields (400),
# list (newest first), delete (200), delete again (404), invalid ids (400),
# empty list, global validation -> 400, unknown route -> 404, wrong method
# -> 405, CORS, data surviving a server restart, DB-down -> 503 clean JSON,
# and cleanup of every test row at the end. Helpers live in the companion
# file test_stage3_lib.ps1.

. "$PSScriptRoot\test_stage3_lib.ps1" -Port $Port

# --- refuse to run if the port is already taken ---------------------------
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
  Write-Output "FAIL  port $Port already in use - free it first"
  exit 1
}

$server = Start-TestServer $null
if (-not $server) {
  Write-Output "FAIL  server never became healthy on $Base - check $ErrLog"
  exit 1
}

$bodyA = '{"destinationId":"test-stage3-alpha","name":"Alpha Beach","location":"Goa, India","category":"beach","latitude":15.5,"longitude":73.7}'
$bodyB = '{"destinationId":"test-stage3-bravo","name":"Bravo Point","category":"attraction"}'

try {
  # --- leftovers from an earlier aborted run ------------------------------
  $leftovers = Remove-TestRows
  if ($leftovers -gt 0) { Write-Output "note  removed $leftovers leftover test row(s) from a previous run" }

  # --- empty list ---------------------------------------------------------
  $r = Invoke-Api "GET" "/api/itinerary"
  if ($r.Code -eq 200 -and [int]$r.Json.count -gt 0) {
    Skip "empty list" "table already has $($r.Json.count) row(s) - empty-state not reproducible"
  } else {
    $emptyOk = ($r.Code -eq 200 -and $null -ne $r.Json -and [int]$r.Json.count -eq 0 -and @($r.Json.data).Count -eq 0)
    Report "empty list -> 200, data [], count 0" $emptyOk "code=$($r.Code) count=$($r.Json.count)"
  }

  # --- add ---------------------------------------------------------------
  $a = Invoke-Api "POST" "/api/itinerary" $bodyA
  $aId = $a.Json.id
  $addOk = ($a.Code -eq 201 -and $a.Json.destinationId -eq "test-stage3-alpha" -and
            $a.Json.name -eq "Alpha Beach" -and $a.Json.location -eq "Goa, India" -and
            $a.Json.category -eq "beach" -and [double]$a.Json.latitude -eq 15.5 -and
            [double]$a.Json.longitude -eq 73.7 -and $null -ne $aId -and $null -ne $a.Json.createdAt)
  Report "add -> 201 saved record" $addOk "code=$($a.Code) id=$aId createdAt=$($a.Json.createdAt)"

  # --- duplicate ----------------------------------------------------------
  $dup = Invoke-Api "POST" "/api/itinerary" $bodyA
  Report "duplicate destinationId -> 409" `
    ($dup.Code -eq 409 -and (Has-ErrorShape $dup.Json) -and $dup.Json.detail -eq "duplicate_destination") `
    "code=$($dup.Code) detail=$($dup.Json.detail)"

  # --- missing / blank / wrong type / malformed bodies --------------------
  $m1 = Invoke-Api "POST" "/api/itinerary" '{"name":"No Id Here"}'
  Report "missing destinationId -> 400" `
    ($m1.Code -eq 400 -and (Has-ErrorShape $m1.Json) -and $m1.Json.detail -eq "missing_field") `
    "code=$($m1.Code) detail=$($m1.Json.detail)"

  $m2 = Invoke-Api "POST" "/api/itinerary" '{"destinationId":"test-stage3-x"}'
  Report "missing name -> 400" `
    ($m2.Code -eq 400 -and (Has-ErrorShape $m2.Json) -and $m2.Json.detail -eq "missing_field") `
    "code=$($m2.Code) detail=$($m2.Json.detail)"

  $m3 = Invoke-Api "POST" "/api/itinerary" '{"destinationId":"test-stage3-x","name":"   "}'
  Report "blank name -> 400" `
    ($m3.Code -eq 400 -and (Has-ErrorShape $m3.Json) -and $m3.Json.detail -eq "blank_field") `
    "code=$($m3.Code) detail=$($m3.Json.detail)"

  $m4 = Invoke-Api "POST" "/api/itinerary" '{"destinationId":12345,"name":"Wrong Type"}'
  Report "wrong-type destinationId -> 400" `
    ($m4.Code -eq 400 -and (Has-ErrorShape $m4.Json) -and $m4.Json.detail -eq "invalid_field_type") `
    "code=$($m4.Code) detail=$($m4.Json.detail)"

  $m5 = Invoke-Api "POST" "/api/itinerary" 'this is not json'
  Report "malformed JSON body -> 400" `
    ($m5.Code -eq 400 -and (Has-ErrorShape $m5.Json) -and $m5.Json.detail -eq "invalid_json") `
    "code=$($m5.Code) detail=$($m5.Json.detail)"

  $m6 = Invoke-Api "POST" "/api/itinerary" ""
  Report "empty body -> 400" `
    ($m6.Code -eq 400 -and (Has-ErrorShape $m6.Json) -and $m6.Json.detail -eq "missing_body") `
    "code=$($m6.Code) detail=$($m6.Json.detail)"

  # --- second row + newest first -----------------------------------------
  $b = Invoke-Api "POST" "/api/itinerary" $bodyB
  $bId = $b.Json.id
  Report "add second row -> 201" ($b.Code -eq 201 -and $null -ne $bId) "code=$($b.Code) id=$bId"

  $r = Invoke-Api "GET" "/api/itinerary"
  $data = @($r.Json.data)
  $testRows = @($data | Where-Object { "$($_.destinationId)".StartsWith($PREFIX) })
  $listOk = ($r.Code -eq 200 -and $testRows.Count -eq 2 -and
             $data[0].destinationId -eq "test-stage3-bravo")
  Report "list -> 200 data+count, newest first" $listOk `
    "code=$($r.Code) count=$($r.Json.count) first=$($data[0].destinationId)"

  # --- delete / delete again / bad ids -----------------------------------
  $d1 = Invoke-Api "DELETE" "/api/itinerary/$aId"
  Report "delete -> 200 message" `
    ($d1.Code -eq 200 -and "$($d1.Json.message)" -like "*deleted*") `
    "code=$($d1.Code) message=$($d1.Json.message)"

  $d2 = Invoke-Api "DELETE" "/api/itinerary/$aId"
  Report "delete again -> 404" `
    ($d2.Code -eq 404 -and (Has-ErrorShape $d2.Json) -and $d2.Json.detail -eq "not_found") `
    "code=$($d2.Code) detail=$($d2.Json.detail)"

  $d3 = Invoke-Api "DELETE" "/api/itinerary/abc"
  Report "non-numeric id -> 400" ($d3.Code -eq 400 -and (Has-ErrorShape $d3.Json)) "code=$($d3.Code)"

  $d4 = Invoke-Api "DELETE" "/api/itinerary/0"
  Report "id 0 -> 400" ($d4.Code -eq 400 -and (Has-ErrorShape $d4.Json)) "code=$($d4.Code)"

  # --- global handlers ----------------------------------------------------
  $g1 = Invoke-Api "GET" "/api/destinations?limit=abc"
  Report "global validation -> 400 not 422" ($g1.Code -eq 400 -and (Has-ErrorShape $g1.Json)) "code=$($g1.Code)"

  $g2 = Invoke-Api "GET" "/health"
  Report "unknown route -> 404 with error shape" ($g2.Code -eq 404 -and (Has-ErrorShape $g2.Json)) "code=$($g2.Code)"

  $g3 = Invoke-Api "PUT" "/api/itinerary"
  Report "wrong method -> 405 with error shape" ($g3.Code -eq 405 -and (Has-ErrorShape $g3.Json)) "code=$($g3.Code)"

  # --- CORS ---------------------------------------------------------------
  $c1 = Invoke-Api "OPTIONS" "/api/itinerary"
  $c2 = Invoke-Api "GET" "/api/itinerary" $null "http://example.com"
  $ao1 = $c1.Headers["Access-Control-Allow-Origin"]
  $ao2 = $c2.Headers["Access-Control-Allow-Origin"]
  Report "CORS preflight allows all origins" ($c1.Code -eq 200 -and $ao1 -eq "*") "code=$($c1.Code) acao=$ao1"
  Report "CORS simple request allows all origins" ($ao2 -eq "*") "acao=$ao2"

  # --- persistence across restart ----------------------------------------
  $r = Invoke-Api "GET" "/api/itinerary"
  $markerBefore = @($r.Json.data | Where-Object { $_.destinationId -eq "test-stage3-bravo" }) | Select-Object -First 1
  Report "marker row present before restart" ($null -ne $markerBefore) "id=$($markerBefore.id)"

  Stop-TestServer $server
  $server = Start-TestServer $null
  if (-not $server) {
    Report "restart server on port $Port" $false "never became healthy - check $ErrLog"
  } else {
    Report "restart server on port $Port" $true "pid=$($server.Id)"
    $r2 = Invoke-Api "GET" "/api/itinerary"
    $markerAfter = @($r2.Json.data | Where-Object { $_.destinationId -eq "test-stage3-bravo" }) | Select-Object -First 1
    $sameCreated = ($null -ne $markerAfter -and "$($markerAfter.createdAt)" -eq "$($markerBefore.createdAt)")
    $persistOk = ($r2.Code -eq 200 -and $null -ne $markerAfter -and
                  $markerAfter.id -eq $markerBefore.id -and $sameCreated)
    Report "row survived server restart" $persistOk `
      "code=$($r2.Code) id=$($markerAfter.id) createdAtUnchanged=$sameCreated"
  }

  # --- cleanup our rows ---------------------------------------------------
  $removed = Remove-TestRows
  $r = Invoke-Api "GET" "/api/itinerary"
  $stillTest = @(@($r.Json.data) | Where-Object { "$($_.destinationId)".StartsWith($PREFIX) })
  Report "cleanup: no test rows remain" ($removed -ge 0 -and $stillTest.Count -eq 0) "removed=$removed"
} finally {
  Stop-TestServer $server
}

# --- database-down phase: expect a clean 503 ------------------------------
# The DSN below points at a port that never accepts connections, so the
# connect fails fast. It is a made-up value, never a real credential.
$unreachable = "postgresql://" + "tripwise" + "@" + "127.0.0.1:1/tripwise"
$broken = Start-TestServer $unreachable
if (-not $broken) {
  Report "start server with unreachable database" $false "check $ErrLog"
} else {
  $b1 = Invoke-Api "GET" "/api/itinerary"
  $b1Ok = ($b1.Code -eq 503 -and (Has-ErrorShape $b1.Json) -and
           $b1.Json.detail -eq "database_unavailable" -and
           $b1.Body -notmatch "Traceback" -and $b1.Body -notmatch "psycopg")
  Report "DB down: list -> 503 clean JSON" $b1Ok "code=$($b1.Code) detail=$($b1.Json.detail)"

  $b2 = Invoke-Api "POST" "/api/itinerary" $bodyA
  $b2Ok = ($b2.Code -eq 503 -and (Has-ErrorShape $b2.Json) -and
           $b2.Body -notmatch "Traceback" -and $b2.Body -notmatch "psycopg")
  Report "DB down: add -> 503 clean JSON" $b2Ok "code=$($b2.Code) detail=$($b2.Json.detail)"
  Stop-TestServer $broken
}

# --- summary --------------------------------------------------------------
Write-Output ""
Write-Output "TOTAL: $script:pass passed, $script:fail failed, $script:skip skipped"
if ($script:fail -gt 0) { exit 1 } else { exit 0 }
