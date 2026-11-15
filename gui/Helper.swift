// Talks to the light helper (agent.py inside Light.app) the same way cli.py does:
// one JSON request per connection over the socket in work/, JSON lines back.
// The panel never touches Bluetooth itself.
import Foundation

struct HelperProblem: Error {
    let text: String
    let notRunning: Bool
}

final class Helper {
    let root: URL  // the folder that holds Lamp.app, Light.app, agent.py and work/

    init(root: URL) {
        self.root = root
        let work = root.appendingPathComponent("work")
        try? FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)
        // Socket paths are limited to 104 bytes, so connect by a short relative name, as cli.py does.
        FileManager.default.changeCurrentDirectoryPath(work.path)
    }

    /// Same value as code_version() in agent.py: the newest whole-second mtime of the *.py files.
    /// A helper started from older code steps aside when it sees a different value.
    func version() -> String {
        var newest = 0
        for name in (try? FileManager.default.contentsOfDirectory(atPath: root.path)) ?? [] where name.hasSuffix(".py") {
            var st = stat()
            if stat(root.appendingPathComponent(name).path, &st) == 0 {
                newest = max(newest, Int(st.st_mtimespec.tv_sec))
            }
        }
        return String(newest)
    }

    private func connectSocket() -> Int32? {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutablePointer(to: &addr.sun_path) { p in
            p.withMemoryRebound(to: CChar.self, capacity: 104) { _ = strncpy($0, "agent.sock", 103) }
        }
        let len = socklen_t(MemoryLayout<sockaddr_un>.size)
        let result = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(fd, $0, len) }
        }
        if result != 0 {
            close(fd)
            return nil
        }
        return fd
    }

    private func startHelper() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        p.arguments = ["-g", "-n", root.appendingPathComponent("Light.app").path, "--args", "agent"]
        try? p.run()
        p.waitUntilExit()
    }

    /// One request. `start: false` never launches the helper: it fails with notRunning instead,
    /// for background checks that must not grab the lamp's Bluetooth link.
    /// Ends after `timeout` seconds without an answer, and after three helper restarts.
    func request(_ req: [String: Any], start: Bool = true, timeout: Int = 90, say: ((String) -> Void)? = nil) throws -> [String: Any] {
        for _ in 0..<3 {
            var fd = connectSocket()
            if fd == nil {
                guard start else { throw HelperProblem(text: "", notRunning: true) }
                startHelper()
                let deadline = Date().addingTimeInterval(45)
                while fd == nil && Date() < deadline {
                    usleep(100_000)
                    fd = connectSocket()
                }
            }
            guard let sock = fd else {
                throw HelperProblem(text: "The Bluetooth helper did not start. Check that Light is allowed under System Settings, Privacy & Security, Bluetooth.", notRunning: true)
            }
            defer { close(sock) }
            var tv = timeval(tv_sec: timeout, tv_usec: 0)
            setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
            var body = req
            body["version"] = version()
            guard var data = try? JSONSerialization.data(withJSONObject: body) else {
                throw HelperProblem(text: "That request could not be written.", notRunning: false)
            }
            data.append(0x0A)
            let sent = data.withUnsafeBytes { write(sock, $0.baseAddress, data.count) }
            guard sent == data.count else { continue }

            var buffer = Data()
            var chunk = [UInt8](repeating: 0, count: 4096)
            var restart = false
            reading: while true {
                let n = read(sock, &chunk, chunk.count)
                if n <= 0 { break }
                buffer.append(contentsOf: chunk[0..<n])
                while let nl = buffer.firstIndex(of: 0x0A) {
                    let line = buffer.subdata(in: buffer.startIndex..<nl)
                    buffer.removeSubrange(buffer.startIndex...nl)
                    guard let msg = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] else { continue }
                    if let text = msg["say"] as? String {
                        say?(text)
                        continue
                    }
                    if msg["restart"] as? Bool == true {
                        restart = true  // an older helper is stepping aside for this version
                        break reading
                    }
                    return msg
                }
            }
            if restart {
                sleep(1)
                continue
            }
            throw HelperProblem(text: "The Bluetooth helper stopped before answering. Try again.", notRunning: false)
        }
        throw HelperProblem(text: "The Bluetooth helper keeps restarting. Details are in work/agent.log.", notRunning: false)
    }
}
