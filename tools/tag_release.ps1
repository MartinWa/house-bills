<#
.SYNOPSIS
Create an annotated release tag, incrementing the patch version by default.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Position = 0)]
    [string]$Version,
    [switch]$Push
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Git {
    $output = & git -C "$PSScriptRoot/.." @args
    if ($LASTEXITCODE -ne 0) {
        throw "git $($args -join ' ') failed (exit code $LASTEXITCODE)."
    }
    $output
}

if ((Invoke-Git branch --show-current) -cne 'main') {
    throw 'Switch to main before creating a release tag.'
}
if (Invoke-Git status --porcelain) {
    throw 'Commit or stash working-tree changes before creating a release tag.'
}

$stablePattern = '(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\z'
$tags = @(Invoke-Git tag --list)
$latest = $tags |
    Where-Object { $_ -cmatch "^v?$stablePattern" } |
    ForEach-Object { [version]($_ -creplace '^v', '') } |
    Sort-Object -Descending |
    Select-Object -First 1

if (-not $Version) {
    if ($null -eq $latest) {
        throw 'No stable release tags found. Use -Version X.Y.Z for the first release.'
    }
    $Version = '{0}.{1}.{2}' -f $latest.Major, $latest.Minor, ($latest.Build + 1)
}
if ($Version -cnotmatch "^$stablePattern") {
    throw 'Release versions must use X.Y.Z without a v prefix or prerelease suffix.'
}
if ($tags -ccontains $Version) {
    throw "Tag $Version already exists."
}
$commit = Invoke-Git rev-parse --verify HEAD
$action = 'Create annotated release tag'
if ($Push) {
    $action += ' and push to origin'
}
if ($PSCmdlet.ShouldProcess("$Version at $commit", $action)) {
    Invoke-Git tag -a $Version -m "Release $Version" $commit
    Write-Host "Created tag $Version."
    if ($Push) {
        try {
            Invoke-Git push --no-follow-tags origin "refs/tags/$Version"
        }
        catch {
            throw "Tag $Version remains local. Retry with: git push --no-follow-tags origin refs/tags/$Version. $($_.Exception.Message)"
        }
        Write-Host "Pushed tag $Version to origin."
    }
}
