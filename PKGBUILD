# Build from this directory (next to nixspec.py): makepkg -si
pkgname=nixspec
pkgver=0.2.0
pkgrel=1
pkgdesc="Terminal configuration editor for Sober (FastFlags and settings)"
arch=('any')
license=('custom')
depends=('python' 'python-textual' 'python-rich')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
optdepends=('flatpak: required for the Launch Sober action')

build() {
  cd "$startdir"
  python -m build --wheel --no-isolation --outdir "$srcdir/dist"
}

package() {
  cd "$startdir"
  python -m installer --destdir="$pkgdir" "$srcdir"/dist/*.whl
  install -Dm644 nixspec.desktop "$pkgdir/usr/share/applications/nixspec.desktop"
  install -Dm644 nixspec.svg "$pkgdir/usr/share/icons/hicolor/scalable/apps/nixspec.svg"
}
