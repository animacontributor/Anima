"""Build the small native/WebView app with SDK tools, without Gradle or global installs."""
from pathlib import Path
import os
import secrets
import subprocess
import sys
import zipfile

PROJECT = Path(__file__).resolve().parent
ROOT = PROJECT.parent
TOOLS = ROOT / ".runtime" / "android-build"
OUT = ROOT / ".runtime" / "phone-app"
OUT.mkdir(parents=True, exist_ok=True)
SDK = Path(os.environ.get("ANIMA_ANDROID_SDK", TOOLS / "sdk"))
JDK = Path(os.environ.get("ANIMA_ANDROID_JDK", TOOLS / "jdk"))
suffix = ".exe" if sys.platform == "win32" else ""
BT = SDK / "build-tools" / "35.0.1"
JAR = SDK / "platforms" / "android-35" / "android.jar"
ENV = dict(os.environ, JAVA_HOME=str(JDK), PATH=str(JDK / "bin") + os.pathsep + os.environ.get("PATH", ""))

def run(args):
    subprocess.run([str(x) for x in args], cwd=PROJECT, env=ENV, check=True)

def main():
    if not JAR.is_file() or not (JDK / "bin" / ("java"+suffix)).is_file():
        raise SystemExit("Android/JDK tools are missing. See PHONE.md for the private build setup.")
    classes = OUT / "classes"; classes.mkdir(exist_ok=True)
    for stale in classes.rglob("*.class"):
        if not stale.resolve().is_relative_to(classes.resolve()):
            raise RuntimeError("Generated class escaped its build directory.")
        stale.unlink()
    generated = OUT / "generated"; generated.mkdir(exist_ok=True)
    dex = OUT / "dex"; dex.mkdir(exist_ok=True)
    resources = OUT / "resources.zip"
    run([BT / ("aapt2"+suffix), "compile", "--dir", PROJECT / "res", "-o", resources])
    unsigned = OUT / "unsigned.apk"
    manifest = PROJECT / "AndroidManifest.xml"
    debug = "--debug" in sys.argv
    if debug:
        manifest = OUT / "test-manifest.xml"
        manifest.write_text((PROJECT / "AndroidManifest.xml").read_text(encoding="utf-8").replace('<application ', '<application android:debuggable="true" '), encoding="utf-8")
    run([BT / ("aapt2"+suffix), "link", "-o", unsigned, "-I", JAR, "--manifest", manifest,
         "--java", generated, "--min-sdk-version", "26", "--target-sdk-version", "35", resources])
    sources = list((PROJECT / "src").rglob("*.java")) + list(generated.rglob("*.java"))
    run([JDK / "bin" / ("javac"+suffix), "-encoding", "UTF-8", "-source", "8", "-target", "8",
         "-classpath", JAR, "-d", classes, *sources])
    run([JDK / "bin" / ("java"+suffix), "-cp", BT / "lib" / "d8.jar", "com.android.tools.r8.D8",
         "--lib", JAR, "--min-api", "26", "--output", dex, *classes.rglob("*.class")])
    with zipfile.ZipFile(unsigned, "a", compression=zipfile.ZIP_DEFLATED) as apk:
        for path in dex.glob("*.dex"): apk.write(path, path.name)
        for path in (PROJECT / "assets").rglob("*"):
            if path.is_file(): apk.write(path, "assets/" + path.relative_to(PROJECT / "assets").as_posix())
    aligned = OUT / "aligned.apk"
    run([BT / ("zipalign"+suffix), "-f", "4", unsigned, aligned])
    key = TOOLS / "local-signing.jks"; password = TOOLS / "signing-password.txt"
    if not key.exists():
        password.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        run([JDK / "bin" / ("keytool"+suffix), "-genkeypair", "-keystore", key,
             "-storepass:file", password, "-keypass:file", password, "-alias", "anima", "-keyalg", "RSA", "-keysize", "3072",
             "-validity", "10000", "-dname", "CN=Anima Phone, O=Local", "-noprompt"])
    final = OUT / ("AnimaPhone-test.apk" if debug else "AnimaPhone.apk")
    secret = password.read_text(encoding="utf-8").splitlines()[0]
    password.write_text(secret + "\n" + secret + "\n", encoding="utf-8")
    run([JDK / "bin" / ("java"+suffix), "-jar", BT / "lib" / "apksigner.jar", "sign", "--ks", key,
         "--ks-key-alias", "anima", "--ks-pass", "file:"+str(password), "--key-pass", "file:"+str(password), "--out", final, aligned])
    run([JDK / "bin" / ("java"+suffix), "-jar", BT / "lib" / "apksigner.jar", "verify", "--verbose", final])
    run([BT / ("aapt2"+suffix), "dump", "badging", final])
    print("APK ready:", final)

if __name__ == "__main__": main()
