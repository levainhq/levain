// levain-sign: a Secure Enclave P-256 signer that emits OpenSSH sk-ecdsa-sha2-nistp256@openssh.com SSH signatures
// (PROTOCOL.u2f + PROTOCOL.sshsig). levain#191 / #153. All output is JSON on stdout.
//   create --out <blob> [--presence]     also writes <blob>.meta.json (format, presence, created): whether the key is
//                                        presence-gated is read from that file at every sign, never from a flag
//   sign   --key <blob> --application ssh: --namespace <ns> --reason <text>   (message on stdin)
//   pubkey --key <blob> --application ssh:
//   status --key <blob>      (zero UI: is the key presence-gated on this Mac?)
// A presence key's access control is .biometryAny + .privateKeyUsage (no device-password fallback). A sign evaluates it
// ONCE, then sets interactionNotAllowed on that context, so no later step of the same sign can show a sheet.
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

// ---- the key's access control: ONE function, used at create and at evaluate ----
func makeAccessControl(presence: Bool) -> SecAccessControl {
    var flags: SecAccessControlCreateFlags = [.privateKeyUsage]
    if presence { flags.insert(.biometryAny) }       // biometrics only: no device-password fallback
    var cferr: Unmanaged<CFError>?
    guard let ac = SecAccessControlCreateWithFlags(kCFAllocatorDefault, kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
                                                   flags, &cferr) else {
        fail("acl", "\(String(describing: cferr?.takeRetainedValue()))")
    }
    return ac
}

// ---- <blob>.meta.json: written at create; the helper reads whether the key is presence-gated from it ----
let metaFormat = "levain-sign-key/1"
func metaPath(_ blob: String) -> String { blob + ".meta.json" }
func writeMeta(_ blob: String, presence: Bool) -> Bool {
    let body: [String: Any] = ["format": metaFormat, "presence": presence,
                               "created": ISO8601DateFormatter().string(from: Date())]
    guard let d = try? JSONSerialization.data(withJSONObject: body, options: [.sortedKeys]) else { return false }
    let fd = open(metaPath(blob), O_WRONLY | O_CREAT | O_EXCL, 0o600)
    if fd < 0 { return false }
    let n = (d + Data("\n".utf8)).withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
    close(fd)
    return n == d.count + 1
}
func readPresence(_ blob: String) -> Bool {
    guard let d = FileManager.default.contents(atPath: metaPath(blob)) else {
        fail("meta_missing", "no \(metaPath(blob)): this key was not made by this helper version")
    }
    guard let o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any], o["format"] as? String == metaFormat,
          let p = o["presence"] as? Bool else { fail("meta_bad", "\(metaPath(blob)) is not a \(metaFormat) file") }
    return p
}

// ---- an error's (domain, code): LAError / OSStatus errors carry them; CryptoKit prints the OSStatus ----
func errorParts(_ e: Error) -> (String, Int?) {
    let ns = e as NSError
    if ns.domain == LAError.errorDomain || ns.domain == NSOSStatusErrorDomain || ns.domain == NSCocoaErrorDomain {
        return (ns.domain, ns.code)
    }
    let d = "\(e)"
    if let re = try? NSRegularExpression(pattern: "error: (-?[0-9]+)"),
       let m = re.firstMatch(in: d, range: NSRange(d.startIndex..., in: d)), let r = Range(m.range(at: 1), in: d),
       let c = Int(d[r]) { return ("CryptoKit.underlyingCoreCryptoError", c) }
    return (ns.domain, nil)
}
func errorExtra(_ e: Error) -> [String: Any] {
    let (dom, code) = errorParts(e)
    var o: [String: Any] = ["error_domain": dom, "error_desc": "\(e)", "error_type": "\(type(of: e))",
                            "error_ns_domain": (e as NSError).domain, "error_ns_code": (e as NSError).code]
    if let c = code { o["error_code"] = c }
    return o
}

// ---- args ----
let argv = Array(CommandLine.arguments.dropFirst())
guard let sub = argv.first else { fail("usage", "subcommand required: create | sign | pubkey | status") }
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
    guard let blob = FileManager.default.contents(atPath: path) else { fail("key_load", "cannot read key blob \(path)") }
    do {
        if let c = context { return try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: blob, authenticationContext: c) }
        return try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: blob)
    } catch { fail("key_load", "\(error)") }
}

let PRESENCE_WALL_SECONDS = 60      // how long one presence sheet waits for a touch
let SIGN_MAX_ATTEMPTS = 3          // the first sign plus the transient-code retries

// ---- counter sidecar: <blob>.counter, 0600, a decimal uint32, incremented per sign under an exclusive flock ----
func nextCounter(_ blobPath: String) -> UInt32 {
    let p = blobPath + ".counter"
    let fd = open(p, O_RDWR | O_CREAT | O_NOFOLLOW, 0o600)
    if fd < 0 { fail("counter", "cannot open counter: errno \(errno)") }
    defer { close(fd) }
    if flock(fd, LOCK_EX) != 0 { fail("counter", "cannot lock counter: errno \(errno)") }
    var buf = [UInt8](repeating: 0, count: 32)
    let n = pread(fd, &buf, buf.count, 0)
    var cur: UInt32 = 0
    if n > 0, let s = String(bytes: buf[0..<n], encoding: .utf8),
       let v = UInt32(s.trimmingCharacters(in: .whitespacesAndNewlines)) { cur = v }
    if cur == UInt32.max { fail("counter", "counter exhausted") }
    let next = cur + 1
    let bytes = Array("\(next)\n".utf8)
    if ftruncate(fd, 0) != 0 || pwrite(fd, bytes, bytes.count, 0) != bytes.count { fail("counter", "cannot write counter: errno \(errno)") }
    fsync(fd)
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
    let ac = makeAccessControl(presence: presence)
    do {
        let key = try SecureEnclave.P256.Signing.PrivateKey(accessControl: ac)
        let fd = open(out, O_WRONLY | O_CREAT | O_EXCL, 0o600)
        if fd < 0 { fail(errno == EEXIST ? "exists" : "write", "cannot create \(out): errno \(errno)") }
        let blob = key.dataRepresentation
        let n = blob.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
        close(fd)
        if n != blob.count { unlink(out); fail("write", "short write") }
        if !writeMeta(out, presence: presence) { unlink(out); unlink(metaPath(out)); fail("write", "cannot write \(metaPath(out))") }
        emit(["ok": true, "public_key": b64(key.publicKey.x963Representation), "presence": presence, "blob": out])
    } catch { fail("create", "\(error)", errorExtra(error)) }

case "pubkey":
    let key = loadKey(need("--key"), context: nil)
    let app = need("--application")
    let line = keyType + " " + b64(openSSHPubBlob(key.publicKey.x963Representation, application: app)) + " levain-sign"
    emit(["ok": true, "line": line])

case "sign":
    let keyPath = need("--key"), app = need("--application"), ns = need("--namespace"), reason = need("--reason")
    if ns.isEmpty { fail("usage", "empty namespace") }
    let presence = readPresence(keyPath)             // from <blob>.meta.json, never from a flag
    let message = FileHandle.standardInput.readDataToEndOfFile()

    // ONE evaluate per sign (counted in "evaluations"), with a PRESENCE_WALL_SECONDS wall; afterwards the context allows no interaction,
    // so nothing later in this sign, a retry included, can show a sheet: it fails instead.
    var ctx: LAContext? = nil
    var evaluations = 0
    if presence {
        let c = LAContext()
        let ac = makeAccessControl(presence: true)
        let sem = DispatchSemaphore(value: 0)
        var ok = false; var err: Error? = nil
        evaluations += 1
        c.evaluateAccessControl(ac, operation: .useKeySign, localizedReason: reason) { r, e in ok = r; err = e; sem.signal() }
        if sem.wait(timeout: .now() + .seconds(PRESENCE_WALL_SECONDS)) == .timedOut {
            c.invalidate()
            fail("presence_timeout", "not touched in time (\(PRESENCE_WALL_SECONDS) s)", ["evaluations": evaluations, "evaluated": false])
        }
        if !ok {
            var extra: [String: Any] = ["evaluations": evaluations, "evaluated": false]
            if let e = err { for (k, v) in errorExtra(e) { extra[k] = v } }
            fail("presence_denied", "\(String(describing: err))", extra)
        }
        c.interactionNotAllowed = true
        ctx = c
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
    var flagsByte: UInt8 = presence ? 0x01 : 0x00
    #if LEVAIN_SIGN_TESTONLY_FORCE_UP
    flagsByte = 0x01      // TEST ONLY: a binary built with -D LEVAIN_SIGN_TESTONLY_FORCE_UP; the shipped build never has this
    #endif
    let counter = nextCounter(keyPath)
    let u2fData = sha(Data(app.utf8)) + Data([flagsByte]) + u32(counter) + sha(signedBlob)

    // Retry the sign only on a named transient OSStatus (same context, never a new evaluate), up to SIGN_MAX_ATTEMPTS attempts in all:
    // errSecIO -36, errSecAllocate -108, errSecInternalComponent -2070 (Security framework result codes). Any other
    // error, an LAError and errSecInteractionNotAllowed -25308 included, is final.
    let transient: Set<Int> = [-36, -108, -2070]
    var attempts = 0
    var sig: P256.Signing.ECDSASignature? = nil
    var lastErr: Error? = nil
    while attempts < SIGN_MAX_ATTEMPTS && sig == nil {
        attempts += 1
        do { sig = try key.signature(for: u2fData) } catch {
            lastErr = error
            guard let c = errorParts(error).1, transient.contains(c), attempts < SIGN_MAX_ATTEMPTS else { break }
            usleep(150_000)
        }
    }
    guard let s = sig else {
        var extra: [String: Any] = ["attempts": attempts, "evaluated": evaluations > 0, "evaluations": evaluations]
        if let e = lastErr { for (k, v) in errorExtra(e) { extra[k] = v } }
        fail("sign_failed", lastErr.map { "\($0)" } ?? "", extra)
    }
    if !key.publicKey.isValidSignature(s, for: u2fData) {
        fail("sign_unverified", "the signature does not verify against the key", ["attempts": attempts, "evaluations": evaluations])
    }
    let raw = s.rawRepresentation   // r || s, 32 bytes each
    let r = raw.prefix(32), sv = raw.suffix(32)
    let inner = mpint(Data(r)) + mpint(Data(sv))
    let sigField = sshString(keyType) + sshString(inner) + Data([flagsByte]) + u32(counter)
    let blob = Data("SSHSIG".utf8) + u32(1) + sshString(pubBlob) + sshString(ns) + sshString("") + sshString(hashAlg) + sshString(sigField)
    let armored = "-----BEGIN SSH SIGNATURE-----\n" + wrap76(b64(blob)) + "\n-----END SSH SIGNATURE-----\n"
    emit(["ok": true, "signature": armored, "counter": Int(counter), "flags": Int(flagsByte), "attempts": attempts,
          "evaluated": evaluations > 0, "evaluations": evaluations, "presence": presence])

case "status":
    // levain#191 R5: ask, with no interface allowed, whether this blob's key needs interaction to sign. A refusal that
    // needs interaction means presence-gated; a signature without any means it is not; a blob this Mac cannot load is
    // "key_load" (not this Mac's). LAContext.interactionNotAllowed: no sheet can appear whatever the key's access control.
    let keyPath = need("--key")
    let ctx = LAContext()
    ctx.interactionNotAllowed = true
    // MEASURED 2026-11-21 (macOS 27, this helper): `levain-sign create --out K --presence` (no sheet), then
    // `levain-sign status --key K` with interactionNotAllowed: no sheet, error type NSError, domain
    // "com.apple.LocalAuthentication", code -1004 (LAError.notInteractive, "User interaction is required."), raised by
    // key.signature(for:); the key loaded fine. A no-presence key (`create --out K2`) signs with no error at all.
    // So "presence-gated" is exactly that (domain, code), matched as numbers; no string search, no other code.
    func needsInteraction(_ e: Error) -> Bool {
        let ns = e as NSError
        return ns.domain == LAError.errorDomain && ns.code == LAError.notInteractive.rawValue   // -1004
    }
    guard let blob = FileManager.default.contents(atPath: keyPath) else { fail("key_load", "cannot read key blob \(keyPath)") }
    let key: SecureEnclave.P256.Signing.PrivateKey
    do { key = try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: blob, authenticationContext: ctx) }
    catch {
        if needsInteraction(error) { emit(["ok": true, "presence_gated": true, "stage": "load"]); exit(0) }
        fail("key_load", "\(error)", errorExtra(error))
    }
    let probe = Data("levain-sign status probe".utf8)
    do { _ = try key.signature(for: probe); emit(["ok": true, "presence_gated": false]) }
    catch {
        if needsInteraction(error) { emit(["ok": true, "presence_gated": true, "stage": "sign"]) }
        else { fail("sign_failed", "\(error)", errorExtra(error)) }
    }

default:
    fail("usage", "unknown subcommand \(sub)")
}
