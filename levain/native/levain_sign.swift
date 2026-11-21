// levain-sign: a Secure Enclave P-256 signer that emits OpenSSH sk-ecdsa-sha2-nistp256@openssh.com SSH signatures
// (PROTOCOL.u2f + PROTOCOL.sshsig). levain#191 / #153 prototype. All output is JSON on stdout.
//   create --out <blob> [--presence]
//   sign   --key <blob> --application ssh: --namespace <ns> [--presence] --reason <text>   (message on stdin)
//   pubkey --key <blob> --application ssh:
import Foundation
import CryptoKit
import LocalAuthentication
import Security

let keyType = "sk-ecdsa-sha2-nistp256@openssh.com"

func emit(_ obj: [String: Any]) {
    let d = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    FileHandle.standardOutput.write(d); FileHandle.standardOutput.write(Data("\n".utf8))
}
func fail(_ code: String, _ msg: String, _ extra: [String: Any] = [:]) -> Never {
    var o: [String: Any] = ["ok": false, "code": code, "error": msg]
    for (k, v) in extra { o[k] = v }
    emit(o); exit(1)
}

// ---- SSH wire encoding ----
func u32(_ v: UInt32) -> Data { Data([UInt8(v >> 24 & 255), UInt8(v >> 16 & 255), UInt8(v >> 8 & 255), UInt8(v & 255)]) }
func sshString(_ d: Data) -> Data { u32(UInt32(d.count)) + d }
func sshString(_ s: String) -> Data { sshString(Data(s.utf8)) }
func mpint(_ d: Data) -> Data {
    var b = Array(d)
    while b.first == 0 { b.removeFirst() }
    if let f = b.first, f & 0x80 != 0 { b.insert(0, at: 0) }
    return sshString(Data(b))
}
func sha(_ d: Data) -> Data { Data(SHA256.hash(data: d)) }

// ---- args ----
let argv = Array(CommandLine.arguments.dropFirst())
guard let sub = argv.first else { fail("usage", "subcommand required: create | sign | pubkey") }
func opt(_ name: String) -> String? {
    guard let i = argv.firstIndex(of: name), i + 1 < argv.count else { return nil }
    return argv[i + 1]
}
func flag(_ name: String) -> Bool { argv.contains(name) }
func need(_ name: String) -> String { guard let v = opt(name) else { fail("usage", "missing \(name)") }; return v }

func openSSHPubBlob(_ x963: Data, application: String) -> Data {
    sshString(keyType) + sshString("nistp256") + sshString(x963) + sshString(application)
}
func loadKey(_ path: String, context: LAContext?) -> SecureEnclave.P256.Signing.PrivateKey {
    guard let blob = FileManager.default.contents(atPath: path) else { fail("key_read", "cannot read key blob \(path)") }
    do {
        if let c = context { return try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: blob, authenticationContext: c) }
        return try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: blob)
    } catch { fail("key_load", "\(error)") }
}

// ---- counter sidecar: <blob>.counter, 0600, a decimal uint32, incremented per sign ----
func nextCounter(_ blobPath: String) -> UInt32 {
    let p = blobPath + ".counter"
    var cur: UInt32 = 0
    if let d = FileManager.default.contents(atPath: p), let s = String(data: d, encoding: .utf8),
       let v = UInt32(s.trimmingCharacters(in: .whitespacesAndNewlines)) { cur = v }
    if cur == UInt32.max { fail("counter", "counter exhausted") }
    let next = cur + 1
    let tmp = p + ".tmp\(getpid())"
    let fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL, 0o600)
    if fd < 0 { fail("counter", "cannot write counter: errno \(errno)") }
    let bytes = Array("\(next)\n".utf8)
    _ = bytes.withUnsafeBufferPointer { write(fd, $0.baseAddress, $0.count) }
    fsync(fd); close(fd)
    if rename(tmp, p) != 0 { unlink(tmp); fail("counter", "cannot replace counter: errno \(errno)") }
    return next
}

func b64(_ d: Data) -> String { d.base64EncodedString() }
func wrap76(_ s: String) -> String {
    var out: [String] = []; var i = s.startIndex
    while i < s.endIndex { let j = s.index(i, offsetBy: 76, limitedBy: s.endIndex) ?? s.endIndex; out.append(String(s[i..<j])); i = j }
    return out.joined(separator: "\n")
}

switch sub {
case "create":
    let out = need("--out")
    let presence = flag("--presence")
    if FileManager.default.fileExists(atPath: out) { fail("exists", "refusing to overwrite \(out)") }
    var flags: SecAccessControlCreateFlags = [.privateKeyUsage]
    if presence { flags.insert(.userPresence) }
    var cferr: Unmanaged<CFError>?
    guard let ac = SecAccessControlCreateWithFlags(kCFAllocatorDefault, kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly, flags, &cferr) else {
        fail("acl", "\(String(describing: cferr?.takeRetainedValue()))")
    }
    do {
        let key = try SecureEnclave.P256.Signing.PrivateKey(accessControl: ac)
        let fd = open(out, O_WRONLY | O_CREAT | O_EXCL, 0o600)
        if fd < 0 { fail(errno == EEXIST ? "exists" : "write", "cannot create \(out): errno \(errno)") }
        let blob = key.dataRepresentation
        let n = blob.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
        close(fd)
        if n != blob.count { unlink(out); fail("write", "short write") }
        emit(["ok": true, "public_key": b64(key.publicKey.x963Representation), "presence": presence, "blob": out])
    } catch { fail("create", "\(error)") }

case "pubkey":
    let key = loadKey(need("--key"), context: nil)
    let app = need("--application")
    let line = keyType + " " + b64(openSSHPubBlob(key.publicKey.x963Representation, application: app)) + " levain-sign"
    emit(["ok": true, "line": line])

case "sign":
    let keyPath = need("--key"), app = need("--application"), ns = need("--namespace"), reason = need("--reason")
    if ns.isEmpty { fail("usage", "empty namespace") }
    let presence = flag("--presence")
    let message = FileHandle.standardInput.readDataToEndOfFile()

    var ctx: LAContext? = nil
    var evaluated = false
    if presence {
        let c = LAContext()
        var cferr: Unmanaged<CFError>?
        guard let ac = SecAccessControlCreateWithFlags(kCFAllocatorDefault, kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
                                                       [.privateKeyUsage, .userPresence], &cferr) else {
            fail("acl", "\(String(describing: cferr?.takeRetainedValue()))")
        }
        let sem = DispatchSemaphore(value: 0)
        var ok = false; var err: Error? = nil
        c.evaluateAccessControl(ac, operation: .useKeySign, localizedReason: reason) { r, e in ok = r; err = e; sem.signal() }
        sem.wait()
        if !ok { fail("presence_denied", "\(String(describing: err))", ["evaluated": false]) }
        ctx = c; evaluated = true
    }
    let key = loadKey(keyPath, context: ctx)
    let x963 = key.publicKey.x963Representation
    let pubBlob = openSSHPubBlob(x963, application: app)

    // PROTOCOL.sshsig section 3: the data the key algorithm signs
    let hashAlg = "sha512"
    let hMsg = Data(SHA512.hash(data: message))
    let signedBlob = Data("SSHSIG".utf8) + sshString(ns) + sshString("") + sshString(hashAlg) + sshString(hMsg)

    // PROTOCOL.u2f: SHA256(application) || flags || counter || [extensions: none] || SHA256(message), message = signedBlob.
    // The device hashes its input with SHA-256, so we hand it the whole u2f blob (CryptoKit signature(for: Data) hashes it).
    let flagsByte: UInt8 = presence ? 0x01 : 0x00
    let counter = nextCounter(keyPath)
    let u2fData = sha(Data(app.utf8)) + Data([flagsByte]) + u32(counter) + sha(signedBlob)

    var attempts = 0
    var sig: P256.Signing.ECDSASignature? = nil
    var lastErr = ""
    let maxAttempts = presence ? 3 : 1
    while attempts < maxAttempts && sig == nil {
        attempts += 1
        do { sig = try key.signature(for: u2fData) } catch { lastErr = "\(error)"; if attempts < maxAttempts { usleep(150_000) } }
    }
    guard let s = sig else { fail("sign_failed", lastErr, ["attempts": attempts, "evaluated": evaluated]) }
    let raw = s.rawRepresentation   // r || s, 32 bytes each
    let r = raw.prefix(32), sv = raw.suffix(32)
    let inner = mpint(Data(r)) + mpint(Data(sv))
    let sigField = sshString(keyType) + sshString(inner) + Data([flagsByte]) + u32(counter)
    let blob = Data("SSHSIG".utf8) + u32(1) + sshString(pubBlob) + sshString(ns) + sshString("") + sshString(hashAlg) + sshString(sigField)
    let armored = "-----BEGIN SSH SIGNATURE-----\n" + wrap76(b64(blob)) + "\n-----END SSH SIGNATURE-----\n"
    emit(["ok": true, "signature": armored, "counter": Int(counter), "flags": Int(flagsByte), "attempts": attempts,
          "evaluated": evaluated, "presence": presence])

default:
    fail("usage", "unknown subcommand \(sub)")
}
