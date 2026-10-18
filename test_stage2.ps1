# Stage 2 smoke tests against a running server (default http://127.0.0.1:8000)
param([string]$Base = "http://127.0.0.1:8000")

$script:pass = 0
$script:fail = 0

function Report($label, $ok, $detail) {
  if ($ok) { $script:pass++; Write-Output "PASS  $label  $detail" }
  else     { $script:fail++; Write-Output "FAIL  $label  $detail" }
}

function Fetch($url) {
  $full = "$Base$url"
  $resp = $null
  try {
    $req = [System.Net.HttpWebRequest]::Create($full)
    $req.Timeout = 40000
    $resp = $req.GetResponse()
  } catch [System.Net.WebException] {
    $resp = $_.Exception.Response
    if (-not $resp) {
      return @{ Code = 0; Body = $_.Exception.Message; Json = $null }
    }
  }
  $code = [int]$resp.StatusCode
  $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
  $body = $reader.ReadToEnd()
  $reader.Close()
  $resp.Close()
  $json = $null
  try { $json = $body | ConvertFrom-Json } catch { $json = $null }
  return @{ Code = $code; Body = $body; Json = $json }
}

# --- 1. health ---------------------------------------------------------------
$r = Fetch "/api/health"
Report "health" ($r.Code -eq 200 -and $r.Json.status -eq "ok") "code=$($r.Code) status=$($r.Json.status)"

# --- 2. happy path list ------------------------------------------------------
$r = Fetch "/api/destinations?city=Goa&limit=5"
$n = @($r.Json.data).Count
$ids = (@($r.Json.data) | ForEach-Object { $_.id }) -join ","
Report "list city=Goa" ($r.Code -eq 200 -and $r.Json.count -gt 0 -and $n -eq $r.Json.count) "code=$($r.Code) count=$($r.Json.count) ids=$($ids.Substring(0, [Math]::Min(70, $ids.Length)))"

# --- 3. attribution ----------------------------------------------------------
$r = Fetch "/api/destinations?city=Panaji&limit=3"
$attr = [string]$r.Json.attribution
Report "attribution in list" ($r.Code -eq 200 -and $attr -match "Geoapify" -and $attr -match "OpenStreetMap") "code=$($r.Code) attribution=$attr"

# --- 4. category filter ------------------------------------------------------
$r = Fetch "/api/destinations?city=Goa&category=restaurant&limit=5"
$cats = (@($r.Json.data) | ForEach-Object { $_.category }) -join ","
Report "category=restaurant" ($r.Code -eq 200) "code=$($r.Code) count=$($r.Json.count) categories=$cats"

# --- 5-9. validation errors --------------------------------------------------
$r = Fetch "/api/destinations?city=Goa&category=zoo"
Report "invalid category -> 400" ($r.Code -eq 400 -and $r.Json.error -like "Invalid category*") "code=$($r.Code) error=$($r.Json.error)"

$r = Fetch "/api/destinations?city=Goa&sortBy=rating"
Report "invalid sortBy -> 400" ($r.Code -eq 400 -and $r.Json.error -like "Invalid sortBy*") "code=$($r.Code) error=$($r.Json.error)"

$r = Fetch "/api/destinations?city=Goa&order=sideways"
Report "invalid order -> 400" ($r.Code -eq 400 -and $r.Json.error -like "Invalid order*") "code=$($r.Code) error=$($r.Json.error)"

$r = Fetch "/api/destinations?city=Goa&limit=99"
Report "limit=99 -> 400" ($r.Code -eq 400 -and $r.Json.error -like "Invalid limit*") "code=$($r.Code) error=$($r.Json.error)"

$r = Fetch "/api/destinations?city=Goa&limit=0"
Report "limit=0 -> 400" ($r.Code -eq 400 -and $r.Json.error -like "Invalid limit*") "code=$($r.Code) error=$($r.Json.error)"

$r = Fetch "/api/destinations"
Report "missing city -> 400" ($r.Code -eq 400 -and $r.Json.error -like "Missing required*") "code=$($r.Code) error=$($r.Json.error)"

# --- 10. un-geocodable city -> empty 200 -------------------------------------
$r = Fetch "/api/destinations?city=zzzznotacity123xyz"
Report "un-geocodable city -> 200 empty" ($r.Code -eq 200 -and $r.Json.count -eq 0 -and @($r.Json.data).Count -eq 0) "code=$($r.Code) count=$($r.Json.count)"

# --- 11. sort by name desc ---------------------------------------------------
$r = Fetch "/api/destinations?city=Goa&sortBy=name&order=desc&limit=10"
$names = @($r.Json.data | ForEach-Object { $_.name })
$sorted = @($names | Sort-Object { $_.ToLower() } -Descending)
$namesOk = ($names.Count -gt 0) -and (($names -join "|") -eq ($sorted -join "|"))
Report "sortBy=name order=desc" ($r.Code -eq 200 -and $namesOk) "code=$($r.Code) first3=$([string]::Join(', ', $names[0..([Math]::Min(2, $names.Count - 1))]))"

# --- 12. sort by distance asc ------------------------------------------------
$r = Fetch "/api/destinations?city=Goa&sortBy=distance&order=asc&limit=10"
$dist = @($r.Json.data | ForEach-Object { $_.distance_m })
$distOk = $dist.Count -gt 0
for ($i = 1; $i -lt $dist.Count; $i++) { if ($dist[$i] -lt $dist[$i - 1]) { $distOk = $false } }
Report "sortBy=distance order=asc" ($r.Code -eq 200 -and $distOk) "code=$($r.Code) distances=$([string]::Join(',', $dist))"

# --- 13. detail with a real id -----------------------------------------------
$listed = Fetch "/api/destinations?city=Goa&limit=3"
$realId = $null
if (@($listed.Json.data).Count -gt 0) { $realId = @($listed.Json.data)[0].id }
if ($realId) {
  $r = Fetch "/api/destinations/$realId"
  $d = $r.Json.data
  Report "detail real id" ($r.Code -eq 200 -and $d.name -and $r.Json.attribution) "code=$($r.Code) name=$($d.name)"
} else {
  Report "detail real id" $false "could not obtain an id from the list"
}

# --- 14. bogus id -> 404 -----------------------------------------------------
$r = Fetch "/api/destinations/does-not-exist-xyz-123"
Report "detail bogus id -> 404" ($r.Code -eq 404 -and $r.Json.error -like "*not found*") "code=$($r.Code) error=$($r.Json.error)"

# --- 15. no API key in any response body -------------------------------------
$r = Fetch "/api/destinations?city=Goa&limit=2"
$leak = $r.Body -match "(?i)apiKey"
Report "no apiKey leak in body" ($r.Code -eq 200 -and -not $leak) "code=$($r.Code) leak=$leak"

Write-Output "-----"
Write-Output "PASSED: $script:pass  FAILED: $script:fail"
if ($script:fail -gt 0) { exit 1 }
