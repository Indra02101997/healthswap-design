#requires -Version 7.0
<#
.SYNOPSIS
  End-to-end smoke test of the HealthSwap reference implementation (RA-1 recommended-add fee + FP-1 fair-premium fee).

.DESCRIPTION
  Runs against a local API started by scripts/run-local.ps1 (or `dotnet run --project src/HealthSwap.Api`) with the demo data
  seeded by `hs seed --demo`. Every step prints PASS/FAIL. The script time-travels the server clock through the admin plane
  (Development only) and resets it at the end.

.EXAMPLE
  pwsh scripts/smoke-test.ps1
#>
param(
    [string]$BaseUrl = "http://localhost:8080",
    [string]$ClientId = "quickbasket-server",
    [string]$ClientSecret = "dev-only-quickbasket-secret-change-me",
    [string]$AdminId = "hs-admin",
    [string]$AdminSecret = "dev-only-admin-secret-change-me"
)

$ErrorActionPreference = "Stop"
$script:failures = 0
function Check([string]$name, [bool]$ok, [string]$detail = "") {
    if ($ok) { Write-Host "PASS  $name" -ForegroundColor Green }
    else { Write-Host "FAIL  $name  $detail" -ForegroundColor Red; $script:failures++ }
}
function Token([string]$id, [string]$secret) {
    (Invoke-RestMethod -Method Post "$BaseUrl/oauth2/token" -ContentType "application/x-www-form-urlencoded" `
        -Body "grant_type=client_credentials&client_id=$id&client_secret=$secret").access_token
}
function Call([string]$method, [string]$path, $body = $null, [hashtable]$headers = $script:partner, [string]$key = $null) {
    $h = @{} + $headers
    if ($key) { $h["Idempotency-Key"] = $key }
    $params = @{ Method = $method; Uri = "$BaseUrl$path"; Headers = $h; SkipHttpErrorCheck = $true; StatusCodeVariable = "sc"; ResponseHeadersVariable = "rh" }
    if ($null -ne $body) { $params.Body = ($body | ConvertTo-Json -Depth 20); $params.ContentType = "application/json" }
    $r = Invoke-RestMethod @params
    $script:lastStatus = [int]$sc
    $script:lastHeaders = $rh
    return $r
}
function Now([int]$plusSeconds = 0) { (Get-Date).ToUniversalTime().AddSeconds($plusSeconds).ToString("o") }

$script:partner = @{ Authorization = "Bearer $(Token $ClientId $ClientSecret)" }
$script:admin = @{ Authorization = "Bearer $(Token $AdminId $AdminSecret)" }
$period = (Get-Date).ToUniversalTime().ToString("yyyy-MM")
$cart = "c_smoke_" + [Guid]::NewGuid().ToString("N").Substring(0, 8)
$user = "u_smoke_" + [Guid]::NewGuid().ToString("N").Substring(0, 8)
Call POST "/admin/clock" @{ offsetSeconds = 0 } $script:admin | Out-Null

Write-Host "`n== 1. Cart evaluation: labeled recommendation ==" -ForegroundColor Cyan
$lines = @(
    @{ lineId = "1"; partnerSku = "SKU-NDL-280"; gtin = "8901234567890"; quantity = 2; unitPrice = @{ amount = "56.00"; currency = "INR" } },
    @{ lineId = "2"; partnerSku = "SKU-COLA-750"; quantity = 1; unitPrice = @{ amount = "40.00"; currency = "INR" } })
$ev = Call POST "/v1/cart-evaluations" @{ cartRef = $cart; userRef = $user; market = "IN"; storeId = "BLR-KOR-012"; currency = "INR"; locale = "en-IN"; lines = $lines }
Check "evaluation returns 200" ($script:lastStatus -eq 200) $script:lastStatus
$noodles = $ev.suggestions | Where-Object lineId -eq "1"
$alt = $noodles.alternatives | Where-Object { $_.product.partnerSku -eq "SKU-MLT-256" } | Select-Object -First 1
Check "noodles line gets the millet noodles swap" ($null -ne $alt)
Check "alternative carries the HealthSwap label" ($alt.recommendation.code -eq "HEALTHSWAP_RECOMMENDED" -and $alt.recommendation.chargeable -eq $true -and $alt.recommendation.feeRule -eq "RA-1")
Check "IN-1 price facts: INR 89.00, delta 33.00, ratio 1.74" ($alt.unitPrice.amount -eq "89.00" -and $alt.unitPriceDelta.amount -eq "33.00" -and $alt.normalizedPriceRatio -eq 1.74)
Check "fee disclosure mentions adding a recommended item" ($ev.disclosures.feeDisclosure -like "*add a recommended item*")
$cola = $ev.suggestions | Where-Object lineId -eq "2"
Check "cola line gets a swap (approved soft drink -> flavoured water map)" ($null -ne $cola) "suggestions: $($ev.suggestions.Count)"
$sid = $noodles.suggestionId

Write-Host "`n== 2. RA-1 events: impression, accept, add, undo, duplicates ==" -ForegroundColor Cyan
$accept = [Guid]::NewGuid()
$r = Call POST "/v1/events" @{ events = @(
    @{ eventId = [Guid]::NewGuid(); type = "IMPRESSION"; suggestionId = $sid; occurredAt = (Now -5) },
    @{ eventId = $accept; type = "ACCEPT"; suggestionId = $sid; alternativePartnerSku = "SKU-MLT-256"; quantity = 2; occurredAt = (Now -3) }) }
Check "events accepted (202)" ($script:lastStatus -eq 202 -and $r.accepted -eq 2) ($r | ConvertTo-Json -Compress)
Check "ACCEPT creates a PENDING INR 1.00 charge" ($r.charges.Count -eq 1 -and $r.charges[0].state -eq "PENDING" -and $r.charges[0].amount.amount -eq "1.00")
$r2 = Call POST "/v1/events" @{ events = @(@{ eventId = $accept; type = "ACCEPT"; suggestionId = $sid; alternativePartnerSku = "SKU-MLT-256"; occurredAt = (Now -3) }) }
Check "replayed eventId is a duplicate and returns the same charge" ($r2.duplicates -eq 1 -and $r2.charges[0].chargeId -eq $r.charges[0].chargeId)
$r3 = Call POST "/v1/events" @{ events = @(@{ eventId = [Guid]::NewGuid(); type = "ADD_TO_CART"; suggestionId = $sid; alternativePartnerSku = "SKU-MLT-256"; quantity = 1; occurredAt = (Now -2) }) }
Check "second add (+1) is another INR 1.00 charge" ($r3.charges.Count -eq 1 -and $r3.charges[0].state -eq "PENDING")
$t = (Get-Date).ToUniversalTime()
$r4 = Call POST "/v1/events" @{ events = @(
    @{ eventId = [Guid]::NewGuid(); type = "ADD_TO_CART"; suggestionId = $sid; alternativePartnerSku = "SKU-MLT-256"; occurredAt = $t.AddSeconds(-1).ToString("o") },
    @{ eventId = [Guid]::NewGuid(); type = "UNDO"; suggestionId = $sid; alternativePartnerSku = "SKU-MLT-256"; occurredAt = $t.AddSeconds(1).ToString("o") }) }
$voided = Call GET "/v1/billing/recommendation-charges/$($r4.charges[0].chargeId)"
Check "ADD_TO_CART undone within 5 s is VOID (UNDONE_WITHIN_GRACE)" ($voided.state -eq "VOID" -and $voided.rejectionReason -eq "UNDONE_WITHIN_GRACE") ($voided | ConvertTo-Json -Compress)
$r5 = Call POST "/v1/events" @{ events = @(@{ eventId = [Guid]::NewGuid(); type = "ADD_TO_CART"; suggestionId = $sid; alternativePartnerSku = "SKU-CRM-500"; occurredAt = (Now) }) }
Check "adding a SKU that was not recommended is rejected" ($r5.rejected[0].code -eq "HS-EVENT-SKU-NOT-RECOMMENDED")

Write-Host "`n== 3. Line labels on the cart after the swap ==" -ForegroundColor Cyan
$ev2 = Call POST "/v1/cart-evaluations" @{ cartRef = $cart; userRef = $user; market = "IN"; storeId = "BLR-KOR-012"; currency = "INR"; lines = @(
    @{ lineId = "1"; partnerSku = "SKU-MLT-256"; quantity = 3; unitPrice = @{ amount = "89.00"; currency = "INR" } }, $lines[1]) }
Check "cart line holding the recommended product is labeled" (($ev2.lineLabels | Where-Object partnerSku -eq "SKU-MLT-256").label.code -eq "HEALTHSWAP_RECOMMENDED")

Write-Host "`n== 4. Product page (PDP) recommendation without an impression ==" -ForegroundColor Cyan
$noodleId = $noodles.original.productId
$pdp = Call GET "/v1/products/$noodleId/alternatives?market=IN&storeId=BLR-KOR-012&cartRef=$cart&placement=PDP"
Check "PDP alternatives mint a suggestionId when a cart is known" ($null -ne $pdp.suggestionId -and $pdp.alternatives.Count -ge 1)
$r6 = Call POST "/v1/events" @{ events = @(@{ eventId = [Guid]::NewGuid(); type = "ADD_TO_CART"; suggestionId = $pdp.suggestionId; alternativePartnerSku = $pdp.alternatives[0].product.partnerSku; occurredAt = (Now) }) }
$noImpressionCharge = $r6.charges[0].chargeId
Check "PDP add creates a PENDING charge (evidence checked at finalization)" ($r6.charges[0].state -eq "PENDING")

Write-Host "`n== 5. FP-1 order attribution (IN-1: fee INR 5.80) ==" -ForegroundColor Cyan
$orderId = "ORD-SMOKE-" + [Guid]::NewGuid().ToString("N").Substring(0, 8)
$order = @{ orderId = $orderId; cartRef = $cart; userRef = $user; storeId = "BLR-KOR-012"; market = "IN"; currency = "INR"; placedAt = (Now); fulfillmentType = "QUICK_COMMERCE"
    orderDiscount = @{ amount = "8.00"; currency = "INR" }
    lines = @(
        @{ lineId = "1"; partnerSku = "SKU-MLT-256"; quantity = 2; unitPrice = @{ amount = "89.00"; currency = "INR" }; allocatedOrderDiscount = @{ amount = "8.00"; currency = "INR" }; netAmount = @{ amount = "170.00"; currency = "INR" }; suggestionId = $sid },
        @{ lineId = "2"; partnerSku = "SKU-COLA-750"; quantity = 1; unitPrice = @{ amount = "40.00"; currency = "INR" }; netAmount = @{ amount = "40.00"; currency = "INR" } }) }
$key = [Guid]::NewGuid().ToString()
$o = Call POST "/v1/orders" $order $script:partner $key
Check "order created (201)" ($script:lastStatus -eq 201) $script:lastStatus
$line1 = $o.attribution.lines | Where-Object lineId -eq "1"
Check "attribution PENDING with fee INR 5.80" ($line1.state -eq "PENDING" -and $line1.fee.amount -eq "5.80") ($line1 | ConvertTo-Json -Compress)
$o2 = Call POST "/v1/orders" $order $script:partner $key
Check "same Idempotency-Key replays the stored response" ($script:lastStatus -eq 201 -and $script:lastHeaders["Idempotent-Replayed"] -eq "true")
$o3 = Call POST "/v1/orders" $order $script:partner ([Guid]::NewGuid().ToString())
Check "same orderId with a new key is a conflict" ($script:lastStatus -eq 409)
$d = Call PATCH "/v1/orders/$orderId" @{ status = "DELIVERED"; occurredAt = (Now) }
Check "delivery accrues the fee" ((($d.attribution.lines | Where-Object lineId -eq "1").state) -eq "ACCRUED")
$adj = Call POST "/v1/orders/$orderId/adjustments" @{ adjustments = @(@{ adjustmentId = "ret-" + [Guid]::NewGuid().ToString("N").Substring(0, 6); lineId = "1"; type = "RETURN"; quantity = 1; occurredAt = (Now) }) }
Check "returning 1 of 2 units reverses INR 2.90" ((($adj.lines | Where-Object lineId -eq "1").fee.amount) -eq "2.90") ($adj | ConvertTo-Json -Depth 5 -Compress)

Write-Host "`n== 6. Settlement: time travel, finalizers, month close ==" -ForegroundColor Cyan
Call POST "/admin/clock" @{ offsetSeconds = 3 * 86400 } $script:admin | Out-Null
$f1 = Call POST "/admin/jobs/finalize-reco-charges" $null $script:admin
$f2 = Call POST "/admin/jobs/finalize-attributions" $null $script:admin
$charges = Call GET "/v1/billing/recommendation-charges?cartRef=$cart&limit=50"
$final = @($charges.items | Where-Object state -eq "FINAL")
Check "two RA-1 charges FINAL (accept + second add)" ($final.Count -eq 2) ($charges.items | ForEach-Object { "$($_.state):$($_.rejectionReason)" }) 
$rej = $charges.items | Where-Object chargeId -eq $noImpressionCharge
Check "PDP add without impression is REJECTED (NO_IMPRESSION)" ($rej.state -eq "REJECTED" -and $rej.rejectionReason -eq "NO_IMPRESSION")
$close = Call POST "/admin/jobs/month-close?period=$period&partnerId=6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01" $null $script:admin
$outcome = @($close.result)[0].outcome
if ($outcome -eq "ALREADY_CLOSED") {
    Write-Host "      NOTE: a statement for $period was already issued by an earlier run; the new charges stay unbilled until the next period." -ForegroundColor Yellow
    Write-Host "      Restart the dev database with --reset (or run scripts/run-local.ps1 -Reset) to replay the billing checks." -ForegroundColor Yellow
    Call POST "/admin/clock" @{ offsetSeconds = 0 } $script:admin | Out-Null
    if ($script:failures -eq 0) { Write-Host "ALL EXECUTED CHECKS PASSED (billing checks skipped)" -ForegroundColor Green; exit 0 } else { exit 1 }
}
Check "month close issued a statement" ($outcome -eq "ISSUED") $outcome
$st = Call GET "/v1/billing/statements?period=$period"
$statement = $st.items | Select-Object -First 1
Write-Host ("      statement: FP-1 {0} - reversals {1} + RA-1 {2} ({3} adds) = net {4}; tax {5}; total {6}" -f $statement.totals.premiumFee.amount, $statement.totals.reversals.amount, $statement.totals.recommendationFee.amount, $statement.totals.recommendationChargeCount, $statement.totals.netFee.amount, $statement.totals.tax.amount, $statement.totals.totalDue.amount)
Check "statement issued with RA-1 and FP-1 totals" ($statement.status -eq "ISSUED" -and [decimal]$statement.totals.recommendationFee.amount -ge 2 -and [decimal]$statement.totals.premiumFee.amount -ge 5.80)
$sc = Call GET "/v1/billing/statements/$($statement.statementId)/recommendation-charges"
Check "statement lists its RA-1 charges" ($sc.items.Count -eq $statement.totals.recommendationChargeCount)

Write-Host "`n== 7. Dispute an RA-1 charge and resolve it ==" -ForegroundColor Cyan
$disp = Call POST "/v1/billing/disputes" @{ statementId = $statement.statementId; reason = "NOT_ADDED_TO_CART"; recommendationChargeIds = @($final[0].chargeId); comment = "Shopper did not add the item" }
Check "dispute opened (201)" ($script:lastStatus -eq 201 -and $disp.status -eq "OPEN")
$res = Call POST "/admin/disputes/$($disp.disputeId)/resolve" @{ decision = "UPHELD"; resolution = "Client logs show a double tap" } $script:admin
Check "dispute upheld with a credit note (INR 1.00 + 18% tax)" ($res.status -eq "UPHELD" -and $res.creditNoteAmount.amount -eq "1.18")
$credited = Call GET "/v1/billing/recommendation-charges/$($final[0].chargeId)"
Check "credited charge shows CREDITED" ($credited.state -eq "CREDITED")
$tb = Call GET "/admin/ledger/trial-balance" $null $script:admin
Check "ledger trial balance nets to zero" ($tb.balanced -eq $true)

Write-Host "`n== 8. Webhooks ==" -ForegroundColor Cyan
Call POST "/admin/jobs/dispatch-webhooks" $null $script:admin | Out-Null
Start-Sleep -Seconds 2
Call POST "/admin/jobs/dispatch-webhooks" $null $script:admin | Out-Null
$hooks = Invoke-RestMethod "$BaseUrl/demo/webhooks/quickbasket"
Check "signed billing.statement.issued webhook received" ((@($hooks | Where-Object { $_.type -eq "billing.statement.issued" -and $_.signatureValid })).Count -ge 1)

Call POST "/admin/clock" @{ offsetSeconds = 0 } $script:admin | Out-Null
Write-Host ""
if ($script:failures -eq 0) { Write-Host "ALL CHECKS PASSED" -ForegroundColor Green; exit 0 } else { Write-Host "$($script:failures) CHECK(S) FAILED" -ForegroundColor Red; exit 1 }
