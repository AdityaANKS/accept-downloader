"""
GitHub Release Downloader

Features:
  - Parse GitHub release URLs
  - List release assets with details
  - Interactive asset picker
  - Download with chunked acceleration
  - Support for latest/specific release
"""

import os
import re
import sys
import json
import time
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, Any
from urllib.parse import urlparse, unquote

import requests

# Try importing project modules
from config import Config as _Cfg
_FILES_DIR = _Cfg.FILES_DIR

from utils import format_size, sanitize_filename, get_logger
from chunked_downloader import ChunkedDownloader

logger = get_logger()


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class ReleaseAsset:
    """A single file asset in a GitHub release."""
    name: str
    download_url: str
    size: int = 0
    content_type: str = ""
    download_count: int = 0
    
    @property
    def extension(self) -> str:
        _, ext = os.path.splitext(self.name)
        return ext.lower()
    
    @property
    def size_str(self) -> str:
        return format_size(self.size)


@dataclass
class Release:
    """A GitHub release."""
    tag_name: str
    name: str
    body: str = ""
    published_at: str = ""
    prerelease: bool = False
    draft: bool = False
    author: str = ""
    assets: List[ReleaseAsset] = field(default_factory=list)
    tarball_url: str = ""
    zipball_url: str = ""
    html_url: str = ""


# ============================================================================
# GITHUB RELEASE PARSER
# ============================================================================

class GitHubReleaseParser:
    """Parse and download GitHub releases."""
    
    API_BASE = "https://api.github.com"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    
    # Patterns to match GitHub URLs
    RELEASE_PATTERNS = [
        # https://github.com/owner/repo/releases/tag/v1.0.0
        re.compile(r'github\.com/([^/]+)/([^/]+)/releases/tag/([^/?#]+)'),
        # https://github.com/owner/repo/releases/latest
        re.compile(r'github\.com/([^/]+)/([^/]+)/releases/latest'),
        # https://github.com/owner/repo/releases
        re.compile(r'github\.com/([^/]+)/([^/]+)/releases/?$'),
        # https://github.com/owner/repo (will fetch latest release)
        re.compile(r'github\.com/([^/]+)/([^/]+)/?$'),
        # Direct download URL
        re.compile(r'github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/(.+)'),
    ]
    
    def __init__(self, token: str = None):
        """
        Args:
            token: Optional GitHub personal access token for higher rate limits
        """
        self.token = token
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': self.USER_AGENT,
            'Accept': 'application/vnd.github+json',
        })
        if token:
            self._session.headers['Authorization'] = f'Bearer {token}'
    
    @classmethod
    def is_github_url(cls, url: str) -> bool:
        """Check if URL is a GitHub URL."""
        return bool(re.search(r'github\.com/[^/]+/[^/]+', url))
    
    @classmethod
    def is_release_url(cls, url: str) -> bool:
        """Check if URL is specifically a GitHub release URL."""
        return bool(re.search(r'github\.com/[^/]+/[^/]+/releases', url))
    
    @classmethod
    def is_direct_download(cls, url: str) -> bool:
        """Check if URL is a direct GitHub release download."""
        return bool(re.search(
            r'github\.com/[^/]+/[^/]+/releases/download/', url
        ))
    
    def parse_url(self, url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Parse a GitHub URL into (owner, repo, tag).
        
        Returns:
            (owner, repo, tag) - tag may be None for latest
        """
        for pattern in self.RELEASE_PATTERNS:
            match = pattern.search(url)
            if match:
                groups = match.groups()
                owner = groups[0]
                repo = groups[1].rstrip('/')
                tag = groups[2] if len(groups) > 2 else None
                return owner, repo, tag
        
        return None, None, None
    
    def get_release(self, owner: str, repo: str, 
                    tag: str = None) -> Optional[Release]:
        """
        Fetch release info from GitHub API.
        
        Args:
            owner: Repository owner
            repo: Repository name
            tag: Release tag (None for latest)
        """
        if tag and tag != 'latest':
            api_url = f"{self.API_BASE}/repos/{owner}/{repo}/releases/tags/{tag}"
        else:
            api_url = f"{self.API_BASE}/repos/{owner}/{repo}/releases/latest"
        
        try:
            resp = self._session.get(api_url, timeout=15)
            
            if resp.status_code == 404:
                logger.warning(f"Release not found: {owner}/{repo} tag={tag}")
                return None
            
            resp.raise_for_status()
            data = resp.json()
            return self._parse_release(data)
        
        except requests.exceptions.HTTPError as e:
            if resp.status_code == 403:
                logger.error("GitHub API rate limit exceeded. "
                           "Use a personal access token for higher limits.")
            else:
                logger.error(f"GitHub API error: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to fetch release: {e}")
            return None
    
    def list_releases(self, owner: str, repo: str, 
                      limit: int = 10) -> List[Release]:
        """List recent releases for a repository."""
        api_url = f"{self.API_BASE}/repos/{owner}/{repo}/releases"
        
        try:
            resp = self._session.get(
                api_url, params={'per_page': limit}, timeout=15
            )
            resp.raise_for_status()
            
            releases = []
            for data in resp.json():
                release = self._parse_release(data)
                releases.append(release)
            
            return releases
        
        except Exception as e:
            logger.error(f"Failed to list releases: {e}")
            return []
    
    def _parse_release(self, data: Dict) -> Release:
        """Parse release JSON from API."""
        assets = []
        for asset_data in data.get('assets', []):
            asset = ReleaseAsset(
                name=asset_data.get('name', ''),
                download_url=asset_data.get('browser_download_url', ''),
                size=asset_data.get('size', 0),
                content_type=asset_data.get('content_type', ''),
                download_count=asset_data.get('download_count', 0),
            )
            assets.append(asset)
        
        author = data.get('author', {})
        
        return Release(
            tag_name=data.get('tag_name', ''),
            name=data.get('name', '') or data.get('tag_name', ''),
            body=data.get('body', ''),
            published_at=data.get('published_at', ''),
            prerelease=data.get('prerelease', False),
            draft=data.get('draft', False),
            author=author.get('login', '') if isinstance(author, dict) else '',
            assets=assets,
            tarball_url=data.get('tarball_url', ''),
            zipball_url=data.get('zipball_url', ''),
            html_url=data.get('html_url', ''),
        )
    
    def download_asset(self, asset: ReleaseAsset,
                       output_dir: str = None,
                       progress_callback=None) -> Dict[str, Any]:
        """
        Download a release asset using chunked downloader.
        
        Args:
            asset: The ReleaseAsset to download
            output_dir: Output directory
            progress_callback: Progress callback
        
        Returns:
            Dict with: status, filepath, filesize
        """
        output_dir = output_dir or os.path.join(_FILES_DIR, "github")
        os.makedirs(output_dir, exist_ok=True)
        
        # Use chunked downloader if available
        if ChunkedDownloader:
            dl = ChunkedDownloader(threads=8, ai_acceleration=True)
            return dl.download(
                url=asset.download_url,
                output_dir=output_dir,
                filename=asset.name,
                progress_callback=progress_callback,
            )
        
        # Fallback: simple download
        filepath = os.path.join(output_dir, asset.name)
        
        try:
            resp = self._session.get(
                asset.download_url, stream=True, timeout=30
            )
            resp.raise_for_status()
            
            total = int(resp.headers.get('Content-Length', 0))
            downloaded = 0
            
            with open(filepath, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    if total > 0:
                        pct = downloaded / total * 100
                        print(f"\r  Downloading: {pct:.1f}% "
                              f"({format_size(downloaded)}/{format_size(total)})  ",
                              end='', flush=True)
            
            print()
            return {
                'status': 'complete',
                'filepath': filepath,
                'filesize': os.path.getsize(filepath),
                'filename': asset.name,
            }
        
        except Exception as e:
            return {'status': 'failed', 'error': str(e)}
    
    def interactive_download(self, url: str, 
                             output_dir: str = None) -> Dict[str, Any]:
        """
        Interactive flow: parse URL → show release → pick asset → download.
        """
        owner, repo, tag = self.parse_url(url)
        if not owner or not repo:
            return {'status': 'failed', 'error': 'Not a valid GitHub URL'}
        
        # Check if it's a direct download URL
        if self.is_direct_download(url):
            asset_name = url.split('/')[-1]
            asset = ReleaseAsset(
                name=unquote(asset_name),
                download_url=url,
            )
            return self.download_asset(asset, output_dir)
        
        print(f"\n  Repository: {owner}/{repo}")
        print(f"  Fetching release{'s' if not tag else f' {tag}'}...")
        
        release = self.get_release(owner, repo, tag)
        if not release:
            # Try listing releases
            releases = self.list_releases(owner, repo, limit=5)
            if not releases:
                return {'status': 'failed', 'error': 'No releases found'}
            
            print(f"\n  Available releases:")
            for i, r in enumerate(releases, 1):
                pre = " (pre-release)" if r.prerelease else ""
                print(f"    {i}. {r.name} [{r.tag_name}]{pre}")
            
            choice = input(f"\n  Select release (1-{len(releases)}): ").strip()
            try:
                idx = int(choice) - 1
                release = releases[idx]
            except (ValueError, IndexError):
                return {'status': 'cancelled'}
        
        # Display release info
        print(f"\n  {'═' * 55}")
        print(f"  Release:   {release.name}")
        print(f"  Tag:       {release.tag_name}")
        if release.published_at:
            pub_date = release.published_at[:10]
            print(f"  Published: {pub_date}")
        if release.prerelease:
            print(f"  ⚠ This is a pre-release")
        print(f"  Assets:    {len(release.assets)}")
        print(f"  {'═' * 55}")
        
        if not release.assets:
            print("\n  No downloadable assets in this release.")
            if release.zipball_url:
                print("  Downloading source code (zip)...")
                source_asset = ReleaseAsset(
                    name=f"{repo}-{release.tag_name}.zip",
                    download_url=release.zipball_url,
                )
                return self.download_asset(source_asset, output_dir)
            return {'status': 'failed', 'error': 'No assets available'}
        
        # List assets
        print(f"\n  Assets:")
        for i, asset in enumerate(release.assets, 1):
            dl_count = f"  [{asset.download_count:,} downloads]" if asset.download_count else ""
            print(f"    {i}. {asset.name}")
            print(f"       {asset.size_str}{dl_count}")
        
        # Source code options
        extra_options = []
        if release.zipball_url:
            extra_options.append(('zip', release.zipball_url))
        if release.tarball_url:
            extra_options.append(('tar.gz', release.tarball_url))
        
        if extra_options:
            base_idx = len(release.assets) + 1
            for j, (ext, _) in enumerate(extra_options):
                print(f"    {base_idx + j}. Source code ({ext})")
        
        # Select
        max_choice = len(release.assets) + len(extra_options)
        choice = input(f"\n  Select asset (1-{max_choice}, or 'a' for all): ").strip()
        
        if choice.lower() == 'a':
            # Download all assets
            results = []
            for asset in release.assets:
                print(f"\n  ➜ {asset.name}")
                result = self.download_asset(asset, output_dir)
                results.append(result)
            return {
                'status': 'complete',
                'results': results,
                'count': len(results),
            }
        
        try:
            idx = int(choice) - 1
            if idx < len(release.assets):
                asset = release.assets[idx]
            elif idx - len(release.assets) < len(extra_options):
                ext, dl_url = extra_options[idx - len(release.assets)]
                asset = ReleaseAsset(
                    name=f"{repo}-{release.tag_name}.{ext}",
                    download_url=dl_url,
                )
            else:
                return {'status': 'cancelled'}
        except (ValueError, IndexError):
            return {'status': 'cancelled'}
        
        print(f"\n  Downloading: {asset.name}")
        return self.download_asset(asset, output_dir)


# ============================================================================
# CONVENIENCE
# ============================================================================

def download_github_release(url: str, output_dir: str = None) -> Dict:
    """Quick GitHub release download."""
    parser = GitHubReleaseParser()
    return parser.interactive_download(url, output_dir)


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    url = input("GitHub URL: ").strip()
    if not url:
        print("No URL provided")
        sys.exit(1)
    
    result = download_github_release(url)
    
    if result.get('status') == 'complete':
        filepath = result.get('filepath', '')
        print(f"\n✓ Saved to: {filepath}")
    else:
        print(f"\n✗ {result.get('error', 'Cancelled')}")
