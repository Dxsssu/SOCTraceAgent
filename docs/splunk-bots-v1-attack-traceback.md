# Splunk BOTS v1 Attack Traceback Notes

Reference repository:

https://github.com/Sean-Everett/Splunk-Boss_of_the_SOC_v1/tree/main

## Scenario 1

This scenario walks through how the attacker group Po1s0n1vy targeted the Wayne Enterprise site `imreallynotbatman.com` and eventually defaced it.

### Reconnaissance and vulnerability scanning

Investigation idea:
Attackers often begin with broad scanning. By searching for global traffic involving the target domain, we can identify unusually active source IPs and inspect HTTP headers to infer attacker tooling.

Search all logs containing `imreallynotbatman.com`:

```text
index="botsv1" imreallynotbatman.com
```

Find the top 10 source IPs:

```text
index="botsv1" imreallynotbatman.com | top limit=10 src_ip
```

After excluding the internal address `192.168.250.70`, two notable IPs remain: `40.80.148.42` and `23.22.63.114`.

`40.80.148.42` accounts for more than 72% of the traffic, which strongly suggests active scanning against the target server.

Inspect HTTP `src_headers` for that IP:

```text
index=botsv1 imreallynotbatman.com sourcetype="stream:http" src_ip="40.80.148.42" | top src_headers
```

This reveals the attacker used the Acunetix Web vulnerability scanner and confirms that the target website used Joomla.

### Exploitation and brute force

Investigation idea:
Once the target was confirmed to be a Web server, the attacker brute-forced the Joomla admin interface. Since login uses HTTP POST, filter on POST traffic to the target host and look for form fields such as `username` and `passwd`.

```text
index="botsv1" sourcetype="stream:http" http_method="POST" dest_ip="192.168.250.70" form_data=*username*passwd*
| stats count by src_ip
```

This highlights source IP `23.22.63.114` as the brute-force origin.

Follow-up ideas:

- Add `| table _time form_data | reverse` to inspect the first attempted passwords
- Use `rex` against `form_data` to extract tried passwords and identify repeated values such as `batman`

### Malicious payload upload

Investigation idea:
After gaining Joomla access, the attacker likely uploaded a backdoor or malicious executable through the admin interface. File uploads commonly use `HTTP POST` with `multipart/form-data`.

```text
index=botsv1 sourcetype=stream:http dest_ip="192.168.250.70" http_method=POST multipart/form-data *.exe
```

This query identifies a malicious file upload named `3791.exe`.

To extract the MD5 hash via Sysmon:

```text
index=botsv1 3791.exe md5 sourcetype="XmlWinEventLog:Microsoft-Windows-Sysmon/Operational" CommandLine="3791.exe"
| rex field="_raw" "MD5=(?<hash>\\w+)"
| table hash
| stats count by hash
```

The resulting MD5 can then be pivoted into external threat-intelligence platforms such as VirusTotal.

### Goal completion and Web defacement

Investigation idea:
Defacement often involves page replacement or external media retrieval. Suricata traffic can reveal the objects returned by the Web server.

```text
index=botsv1 sourcetype="suricata" src_ip="192.168.250.70" dest_ip="23.22.63.114"
| stats count by http.http_method, http.hostname, http.url
| sort -count
```

This workflow reveals the image `poisonivy-is-coming-for-you-batman.jpeg`.

Further inspection shows that the image was hosted on the dynamic DNS domain `prankglassinebracket.jumpingcrab.com` over non-standard port `1337`, and that the domain resolved back to the brute-force IP `23.22.63.114`.

## Scenario 2

Scenario 2 is harder to reconstruct quickly, so the current focus remains on Scenario 1.

Useful external walkthrough:

https://medium.com/@m00hamedw0rk/ransomware-investigation-using-splunk-botsv1-scenario-2-walkthrough-23465aa33c71

### Background

On August 24, 2016, employee Bob Smith's Windows 10 workstation `we8105desk` was infected with Cerber ransomware, causing both local files and network shares to be encrypted.

### Initial access

Core fact:
The attack did not begin with phishing. Bob Smith plugged in a malicious USB drive found in the parking lot and opened the macro-enabled Office document `Miranda_Tate_unveiled.dotm`.

Useful validation steps:

```text
index=botsv1 source="WinEventLog:Microsoft-Windows-Sysmon/Operational" host=we8105desk EventID=3
| top limit=5 SourceIp
```

This confirms the key workstation IP as `192.168.250.100`.

USB-friendly names can be found in the registry data:

```text
index=botsv1 sourcetype=winregistry friendlyname
```

This identifies the inserted USB drive as `MIRANDA_PRI`.

### Execution

Core fact:
After the document was opened, a malicious macro silently launched a VBS payload that dropped and invoked `121214.tmp`, which started the ransomware execution chain.

Useful Sysmon pivot:

```text
index=botsv1 source=WinEventLog:Microsoft-Windows-Sysmon/Operational host=we8105desk vbs
| eval vbslen=len(CommandLine)
| table CommandLine vbslen
```

To trace the dropped executable:

```text
index=botsv1 host=we8105desk EventID=1 CommandLine=*121214.tmp* | table _time ProcessId ParentProcessId
```

### C2 and follow-on network activity

Core fact:
After local startup, the ransomware reached out to malicious DNS infrastructure to connect to C2 and download the actual Cerber payload. Suricata and FortiGate logs contain the supporting evidence.

Useful checks:

```text
index=botsv1 sourcetype=suricata cerber
| stats count by alert.signature_id
```

```text
index=botsv1 sourcetype=stream:dns src_ip=192.168.250.100 "query_type{}"=A NOT (query{}=*.microsoft.com OR query{}=*.google.com OR query{}=*.waynecorpinc.com)
| table _time query{} src dest
```

After excluding common benign domains, one of the first suspicious domains is `solidaritedeproximite.org`.
