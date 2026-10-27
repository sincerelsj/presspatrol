# 필요적 게재사항(신문법 제21조) 실태점검 — 전 과정 실행
#
# 이 파일은 UTF-8 BOM 으로 저장돼 있어야 한다. BOM 이 없으면 PowerShell 5.1 이 시스템
# ANSI(cp949)로 읽어 한글이 깨지고 파싱이 실패한다. 편집 후 저장할 때 인코딩을 확인해라.
#
# 사용 (저장소 루트에서):
#   .\ver_1\run_full.ps1 -InputXlsx ver_1\input\pilot4.xlsx -WorkDir ver_1\run_pilot4 -Fresh
#
# 옵션
#   -Fresh          작업폴더를 비우고 처음부터 (직접 지울 필요 없다)
#   -SkipCapture    캡처 생략, 저장된 shots\ 재사용. 규칙만 돌린 폴더에 판단만 이어붙일 때
#   -SkipLlm        API 호출 0 (규칙·캡처·증빙만)
#   -NoVision       비전 진단만 끔
#   -SkipResolve    접속 불가 건 제호 검색만 끔
#   -Shots          all(기본) | flagged | diagnose | none
#   -DryRun         계획만 출력 (API 0)
#   -Yes            비용 확인 대기 없이 바로 진행
#
# 전제: 저장소 루트 .env 에 ANTHROPIC_API_KEY. 배치 API 는 쓰지 않는다(순차 호출).

param(
    [string]$InputXlsx = "ver_1\input\pilot4.xlsx",
    [string]$WorkDir   = "ver_1\run_pilot4",
    [string]$BaseDate  = "",
    [int]$Workers      = 4,
    [ValidateSet("flagged","all","diagnose","none")]
    [string]$Shots     = "all",
    [switch]$Fresh,
    [switch]$SkipCapture,
    [switch]$SkipLlm,
    [switch]$NoVision,
    [switch]$SkipResolve,
    [switch]$DryRun,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Root    = Split-Path -Parent $PSScriptRoot
$Scripts = Join-Path $PSScriptRoot "skill\scripts"
Set-Location $Root

# --- 전제 확인 -----------------------------------------------------------
if (-not (Test-Path $InputXlsx)) {
    Write-Host "[중단] 입력 파일이 없다: $InputXlsx" -ForegroundColor Red
    Get-ChildItem "ver_1\input\*.xlsx" -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host ("        있는 파일: " + $_.FullName) }
    exit 1
}
if (-not (Test-Path (Join-Path $Scripts "orchestrate.py"))) {
    Write-Host "[중단] 정본 스크립트를 찾지 못했다: $Scripts" -ForegroundColor Red
    exit 1
}
if ($BaseDate -eq "") { $BaseDate = (Get-Date).ToString("yyyy-MM-dd") }

# API 키는 .env 또는 환경변수. 판단 단계를 돌릴 거면 미리 막는다 —
# R0/R0b 를 다 돌린 뒤 R1 에서 죽으면 캡처를 또 해야 한다.
if (-not $SkipLlm -and -not $DryRun) {
    $hasKey = $false
    if ($env:ANTHROPIC_API_KEY) { $hasKey = $true }
    $envFile = Join-Path $Root ".env"
    if (-not $hasKey -and (Test-Path $envFile)) {
        if (Select-String -Path $envFile -Pattern "^\s*ANTHROPIC_API_KEY\s*=\s*\S" -Quiet) {
            $hasKey = $true
        }
    }
    if (-not $hasKey) {
        Write-Host "[중단] ANTHROPIC_API_KEY 를 찾지 못했다." -ForegroundColor Red
        Write-Host "       .env 에 ANTHROPIC_API_KEY=sk-ant-... 를 넣거나" -ForegroundColor DarkGray
        Write-Host '       $env:ANTHROPIC_API_KEY = "sk-ant-..." 로 설정해라' -ForegroundColor DarkGray
        Write-Host "       API 없이 규칙만 돌리려면 -SkipLlm 을 붙여라" -ForegroundColor DarkGray
        exit 1
    }
}

# --- 대상 건수·비용 추정 --------------------------------------------------
Write-Host ""
Write-Host "=== 필요적 게재사항 점검 (신문법 제21조) ===" -ForegroundColor Cyan
Write-Host "  입력      : $InputXlsx"
Write-Host "  작업폴더  : $WorkDir"
Write-Host "  기준일    : $BaseDate"
Write-Host "  스크립트  : $Scripts (정본)"
Write-Host "  스크린샷  : $Shots"
Write-Host "  모델      : claude-sonnet-5 (DISCLOSURE_LLM_MODEL 로 변경 가능)"
Write-Host "  호출방식  : 순차 (배치 API 미사용)"
if ($SkipLlm)      { Write-Host "  * -SkipLlm      : API 호출 0" -ForegroundColor DarkYellow }
if ($SkipCapture)  { Write-Host "  * -SkipCapture  : 저장된 shots\ 재사용, 사이트 방문 없음" -ForegroundColor DarkYellow }
if ($NoVision)     { Write-Host "  * -NoVision     : 비전 진단 끔" -ForegroundColor DarkYellow }
if ($SkipResolve)  { Write-Host "  * -SkipResolve  : 접속 불가 제호 검색 끔" -ForegroundColor DarkYellow }
Write-Host ""

# --- 0단계: 컬럼 매핑 확인 -----------------------------------------------
Write-Host "[1/3] 컬럼 매핑 확인" -ForegroundColor Yellow
python (Join-Path $Scripts "inspect_registry.py") $InputXlsx
if ($LASTEXITCODE -ne 0) {
    Write-Host "[중단] 컬럼 매핑 실패. url 컬럼을 못 찾으면 --map 으로 지정해야 한다." -ForegroundColor Red
    exit 1
}
Write-Host ""
Write-Host "  위 mapping 의 url 이 맞는지 확인해라." -ForegroundColor DarkGray

# 건수를 읽어 비용을 추정한다 (pilot3 실측 건당 약 0.031 USD)
$rowCount = 0
try {
    $probe = python -c "import sys,json;sys.path.insert(0,r'$Scripts');from md_core.registry import load_registry;r,_,_=load_registry(r'$InputXlsx');print(len(r))"
    if ($?) { $rowCount = [int]$probe }
} catch { $rowCount = 0 }

if (-not $SkipLlm -and -not $DryRun -and $rowCount -gt 0) {
    $est = [math]::Round($rowCount * 0.031 + 0.15, 2)
    Write-Host ""
    Write-Host ("  대상 " + $rowCount + "건, 예상 API 비용 약 $" + $est + " (pilot3 실측 건당 0.031 기준)") -ForegroundColor Cyan
    if (-not $Yes) {
        Write-Host "  중단하려면 지금 Ctrl+C. 8초 후 시작한다." -ForegroundColor DarkGray
        Start-Sleep -Seconds 8
    }
}

# --- 작업폴더 초기화 -----------------------------------------------------
if ($Fresh -and (Test-Path $WorkDir)) {
    Write-Host ""
    Write-Host "[초기화] $WorkDir 의 기존 산출물을 지운다" -ForegroundColor DarkYellow
    Remove-Item -Path $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

# --- 1단계: 전 과정 -----------------------------------------------------
Write-Host ""
Write-Host "[2/3] 전 과정 실행" -ForegroundColor Yellow
Write-Host "      R0  규칙추출 - 게이트A" -ForegroundColor DarkGray
Write-Host "      R0b 캡처 (단일 방문: 스크린샷/렌더HTML/기사페이지)" -ForegroundColor DarkGray
Write-Host "      R0c 렌더HTML 반영 재추출 - 게이트A 재평가" -ForegroundColor DarkGray
Write-Host "      R1b 접속불가 제호검색 / R1 텍스트검토 - 게이트B" -ForegroundColor DarkGray
Write-Host "      R2  비전진단 / R3 발행일검증  (둘 다 방문 없음)" -ForegroundColor DarkGray
Write-Host "      R4  주석본 + 근거위치(evidence)" -ForegroundColor DarkGray
Write-Host "      병합 - 대조 - 최종xlsx - 위치지도 - 결과점검" -ForegroundColor DarkGray
Write-Host ""

$cmdArgs = @(
    (Join-Path $Scripts "orchestrate.py")
    "--workdir";   $WorkDir
    "--input";     $InputXlsx
    "--base-date"; $BaseDate
    "--workers";   "$Workers"
    "--shots";     $Shots
)
if ($DryRun)      { $cmdArgs += "--dry-run" }
if ($SkipLlm)     { $cmdArgs += "--skip-llm" }
if ($NoVision)    { $cmdArgs += "--no-vision" }
if ($SkipCapture) { $cmdArgs += "--skip-capture" }
if ($SkipResolve) { $cmdArgs += "--skip-resolve" }

python @cmdArgs
$rc = $LASTEXITCODE
if ($rc -ne 0) {
    Write-Host ""
    Write-Host "[실패] orchestrate.py exit $rc" -ForegroundColor Red
    Write-Host "  중간 산출물은 남아 있다. 캡처는 이미 끝났으니 판단만 이어붙이려면:" -ForegroundColor DarkGray
    Write-Host ("    .\ver_1\run_full.ps1 -InputXlsx " + $InputXlsx + " -WorkDir " + $WorkDir + " -SkipCapture -Yes")
    exit $rc
}
if ($DryRun) {
    Write-Host ""
    Write-Host "계획만 출력했다 (-DryRun). API 호출 0." -ForegroundColor Green
    exit 0
}

# --- 2단계: 산출물 -----------------------------------------------------
Write-Host ""
Write-Host "[3/3] 산출물" -ForegroundColor Yellow
$final  = Join-Path $WorkDir ("outputs\final\" + $BaseDate + "_필요적게재사항_점검결과.xlsx")
$map    = Join-Path $WorkDir ("outputs\위치지도_" + $BaseDate + ".xlsx")
$verify = Join-Path $WorkDir ("outputs\verify_" + $BaseDate + ".csv")
$report = Join-Path $WorkDir ("outputs\점검보고_" + $BaseDate + ".md")

foreach ($f in @($final, $map, $verify, $report)) {
    if (Test-Path $f) {
        $kb = [math]::Round((Get-Item $f).Length / 1KB, 1)
        Write-Host ("  OK  " + $f + "  (" + $kb + " KB)") -ForegroundColor Green
    } else {
        Write-Host ("  --  " + $f + "  (없음)") -ForegroundColor DarkYellow
    }
}

$shotDir = Join-Path $WorkDir "shots"
$evDir   = Join-Path $WorkDir "evidence"
if (Test-Path $shotDir) {
    $png  = (Get-ChildItem $shotDir -Filter "*_footer.png" -ErrorAction SilentlyContinue).Count
    $art  = (Get-ChildItem $shotDir -Filter "*_article.html" -ErrorAction SilentlyContinue).Count
    Write-Host ("  증빙: 하단 스크린샷 " + $png + "건 / 기사페이지 " + $art + "건")
}
if (Test-Path $evDir) {
    $ev = (Get-ChildItem $evDir -Filter "*.json" -ErrorAction SilentlyContinue).Count
    Write-Host ("  근거위치: evidence\*.json " + $ev + "건")
}

Write-Host ""
Write-Host "=== 검수 시 볼 것 ===" -ForegroundColor Cyan
Write-Host "  준수여부_LLM 이 최종 판정이다 (준수여부_규칙 은 대조용 동결 스냅샷)."
Write-Host "  규칙_LLM_차이 로 늘고(+) 줄고(-) 바뀐(~) 항목을 한눈에 본다."
Write-Host "  확인필요=Y 는 사람 검증 전까지 확정이 아니다."
Write-Host "  등록정보상이 시트는 제9조 변경등록 사안이다 - 제21조 위반이 아니다."
Write-Host "  점검보고 md 에 7차원 판정과 확정 결함이 정리돼 있다."
Write-Host "  위치지도 xlsx 의 CSS_selector 로 다음 회차에 바로 확인할 수 있다."
Write-Host "  스크린샷 오른쪽 위에 캡처 시각이 찍혀 있다 (증빙 시점 확인용)."
Write-Host ""
Write-Host "완료." -ForegroundColor Green
