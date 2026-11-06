/* fire_fakeclaude.c: a FAKE vendor CLI for the S10b VM fire rig (observation only; speaks no Anthropic protocol).
 *   --version              -> "2.1.316 (Claude Code)"
 *   auth status --json     -> a logged-in answer for a throwaway identity
 *   anything else (a run)  -> reads HTTPS_PROXY, opens ONE CONNECT tunnel to api.anthropic.com:443 through the
 *                             slot's front, sends a VALID TLS ClientHello (SNI api.anthropic.com; the front refuses anything else as
 *                             malformed and counts nothing) then 20000 junk bytes through it (so the front counts tunnel bytes),
 *                             writes a note to $TMPDIR/fake_ran.txt, then holds HOLD_S seconds (SIGTERM exits 143).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

/* vmend: HOLD_S is overridable (-DHOLD_S=25) so a root planter can write into the var-folders dir while the session is live. */
#ifndef HOLD_S
#define HOLD_S 5
#endif

/* A minimal valid TLS ClientHello record (TLS 1.2 legacy_version, one cipher suite, null compression, one server_name
 * extension) for `host`. Returns its length. */
static size_t build_hello(unsigned char *o, const char *host) {
    size_t hl = strlen(host), n = 0;
    size_t sn = 2 + 1 + 2 + hl, ext = 4 + sn, body = 2 + 32 + 1 + 2 + 2 + 2 + 2 + ext, hs = 4 + body;
    o[n++] = 0x16; o[n++] = 0x03; o[n++] = 0x01; o[n++] = (hs >> 8) & 255; o[n++] = hs & 255;
    o[n++] = 0x01; o[n++] = (body >> 16) & 255; o[n++] = (body >> 8) & 255; o[n++] = body & 255;
    o[n++] = 0x03; o[n++] = 0x03;
    for (int i = 0; i < 32; i++) o[n++] = (unsigned char)(i + 1);
    o[n++] = 0;                                   /* session id */
    o[n++] = 0; o[n++] = 2; o[n++] = 0x13; o[n++] = 0x01;   /* cipher suites */
    o[n++] = 1; o[n++] = 0;                       /* compression: null */
    o[n++] = (ext >> 8) & 255; o[n++] = ext & 255;
    o[n++] = 0; o[n++] = 0; o[n++] = (sn >> 8) & 255; o[n++] = sn & 255;       /* server_name */
    o[n++] = ((sn - 2) >> 8) & 255; o[n++] = (sn - 2) & 255; o[n++] = 0;
    o[n++] = (hl >> 8) & 255; o[n++] = hl & 255;
    memcpy(o + n, host, hl); n += hl;
    return n;
}

static volatile sig_atomic_t term = 0;
static void on_term(int s) { (void)s; term = 1; }

int main(int argc, char **argv) {
    if (argc >= 2 && strcmp(argv[1], "--version") == 0) { puts("2.1.316 (Claude Code)"); return 0; }
    if (argc >= 3 && strcmp(argv[1], "auth") == 0 && strcmp(argv[2], "status") == 0) {
        puts("{\"loggedIn\":true,\"orgId\":\"123e4567-e89b-12d3-a456-426614174000\",\"email\":\"fake@example.invalid\"}");
        return 0;
    }
    signal(SIGTERM, on_term);
    char note[2048]; int n = 0;
    const char *tmp = getenv("TMPDIR"); const char *proxy = getenv("HTTPS_PROXY");
    n += snprintf(note + n, sizeof note - n, "argc=%d HTTPS_PROXY=%s TMPDIR=%s uid=%d\n", argc, proxy ? proxy : "(none)", tmp ? tmp : "(none)", (int)getuid());
    long sent = 0, got = 0; char head[256] = "";
    if (proxy) {
        const char *p = strrchr(proxy, ':');
        int port = p ? atoi(p + 1) : 0;
        int fd = socket(AF_INET, SOCK_STREAM, 0);
        struct sockaddr_in a; memset(&a, 0, sizeof a);
        a.sin_family = AF_INET; a.sin_port = htons(port); inet_pton(AF_INET, "127.0.0.1", &a.sin_addr);
        if (connect(fd, (struct sockaddr *)&a, sizeof a) == 0) {
            const char *req = "CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com:443\r\n\r\n";
            write(fd, req, strlen(req));
            ssize_t r = read(fd, head, sizeof head - 1);
            if (r > 0) { head[r] = 0; got += r; }
            char *cr = strchr(head, '\r'); if (cr) *cr = 0;
            if (strstr(head, " 200")) {
                unsigned char hello[256]; size_t hn = build_hello(hello, "api.anthropic.com");
                { ssize_t w = write(fd, hello, hn); if (w > 0) sent += w; }
                char junk[1000]; memset(junk, 0x16, sizeof junk);
                for (int i = 0; i < 20; i++) { ssize_t w = write(fd, junk, sizeof junk); if (w > 0) sent += w; }
                char buf[4096]; ssize_t g;
                alarm(0);
                struct timeval tv = {3, 0};
                setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
                while ((g = read(fd, buf, sizeof buf)) > 0) got += g;
            }
        } else {
            snprintf(head, sizeof head, "connect-failed");
        }
        close(fd);
    }
    n += snprintf(note + n, sizeof note - n, "first=%s sent=%ld got=%ld\n", head, sent, got);
    if (tmp) {
        char path[512]; snprintf(path, sizeof path, "%s/fake_ran.txt", tmp);
        FILE *f = fopen(path, "w"); if (f) { fputs(note, f); fclose(f); }
    }
    {   /* vsun rig addition: write files into the slot uid's /private/var/folders/<xx>/<hash> subtree (its DARWIN_USER_DIR is <hash>/0/):
         * under 0/, T/, C/ and the <hash> dir itself. The outcome of each is appended to the note (stderr = vendor.err). */
        char ud[1024]; size_t m = confstr(_CS_DARWIN_USER_DIR, ud, sizeof ud);
        if (m == 0 || m > sizeof ud) { n += snprintf(note + n, sizeof note - n, "vf-write: confstr(USER_DIR) failed\n"); }
        else {
            n += snprintf(note + n, sizeof note - n, "vf-write: DARWIN_USER_DIR=%s\n", ud);
            const char *rel[4] = { "vsun_child_in_0.txt", "../T/vsun_child_in_T.txt", "../C/vsun_child_in_C.txt", "../vsun_child_in_hash.txt" };
            for (int i = 0; i < 4; i++) {
                char path[1300]; snprintf(path, sizeof path, "%s%s", ud, rel[i]);
                FILE *f = fopen(path, "w");
                if (f) { fputs("written by the fake vendor child\n", f); fclose(f); n += snprintf(note + n, sizeof note - n, "vf-write: %s OK\n", path); }
                else { n += snprintf(note + n, sizeof note - n, "vf-write: %s FAILED errno=%d\n", path, errno); }
            }
        }
    }
#ifdef VSUN_VF
    {   /* vsun rig addition 2: the confstr route above fails inside the product's sandbox/env, so write by the baked-in absolute path of the slot
         * uid's /private/var/folders/<xx>/<hash> (VSUN_VF, set at seed time): under 0/, T/, C/ and the hash dir itself. */
        const char *rel2[4] = { "/0/vsun_child_in_0.txt", "/T/vsun_child_in_T.txt", "/C/vsun_child_in_C.txt", "/vsun_child_in_hash.txt" };
        for (int i = 0; i < 4; i++) {
            char path[1300]; snprintf(path, sizeof path, "%s%s", VSUN_VF, rel2[i]);
            FILE *f = fopen(path, "w");
            if (f) { fputs("written by the fake vendor child\n", f); fclose(f); n += snprintf(note + n, sizeof note - n, "vf-write2: %s OK\n", path); }
            else { n += snprintf(note + n, sizeof note - n, "vf-write2: %s FAILED errno=%d\n", path, errno); }
        }
    }
#endif
    {   /* vmend: the /usr/bin/git shim (xcrun) caches under DARWIN_USER_CACHE_DIR, this uid's var-folders C/. Run it as a vendor would and
         * record its status and first line: a denied cache write that the shim survives is a benign hit the sandbox log will show. */
        FILE *gp = popen("/usr/bin/git --version 2>&1", "r");
        char gl[300] = "";
        if (gp) {
            if (!fgets(gl, sizeof gl, gp)) gl[0] = 0;
            int grc = pclose(gp);
            char *nl = strchr(gl, 10); if (nl) *nl = 0;
            n += snprintf(note + n, sizeof note - n, "git-shim: status=%d first=%s\n", grc, gl);
        } else { n += snprintf(note + n, sizeof note - n, "git-shim: popen failed errno=%d\n", errno); }
    }
    fputs(note, stderr);
    for (int i = 0; i < HOLD_S && !term; i++) sleep(1);
    return term ? 143 : 0;
}
