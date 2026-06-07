# Splunk Bots v1 攻击溯源

https://github\.com/Sean\-Everett/Splunk\-Boss\_of\_the\_SOC\_v1/tree/main

## 场景一 

展示了攻击组织 Po1s0n1vy 对 Wayne Enterprise 的网站 `imreallynotbatman.com` 进行攻击并最终篡改网页的完整过程。

### 侦察与漏洞扫描

**溯源思路：**攻击者在发起攻击前通常会对目标进行大规模的扫描。通过检索目标域名的全局流量，可以找出请求量异常高的源 IP，并分析 HTTP 请求头（Headers）来识别攻击者使用的工具。

使用下面的语句查询所有包含关键字 `imreallynotbatman.com` 的日志 

```Plain Text
index="botsv1" imreallynotbatman.com
```

![Image](https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/authcode/?code=NDJlYTAyNTc0ZGYwYmE2NWU4MGJkYTBiNTcwYmJmYzBfZDFmNTU5YjUyOTY2MzY5YjI5Y2Y2OGUxNjM2YzFkYzhfSUQ6NzY0NjY4ODY0OTM1MDQ4MjkwMF8xNzgwODUyMjk3OjE3ODA5Mzg2OTdfVjM)

统计出现次数最多的前10个源IP地址。

```Plain Text
index="botsv1" imreallynotbatman.com | top limit=10 src_ip
```

![Image](https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/authcode/?code=N2Q2NGIyNDE1YjA0YTk2YjNiOGM4YjVjMmRlY2Q1NGRfNTI1MDlkZWVmNjE5ZGFiYTViOTc3MjIwYWIxMjhhZGZfSUQ6NzY0NjY4OTE2OTMzODg5NTI4OF8xNzgwODUyMjk3OjE3ODA5Mzg2OTdfVjM)

排除内网地址 `192.168.250.70` 后，还存在 `40.80.148.42` 和 `23.22.63.114`。

其中 `40.80.148.42` 占比超过 72%，由于流量巨大，这个可以认为 IP 地址正在扫描我们的服务器。

通过进一步展开该 IP 对应http流的 `src_headers` 字段数据：

```Plain Text
index=botsv1 imreallynotbatman.com sourcetype="stream:http" src_ip="40.80.148.42" | top src_headers
```

![Image](https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/authcode/?code=ZjMzMDIwMmFhMzEwYTkwOWE5NjM5NjAzOTliZGUwY2ZfM2Q4NGI0NjU1MTFmM2FkNjY2ZTEzMjgxMTA2YTJiY2ZfSUQ6NzY0NjY5NDYyNjA3NDA4NjYwNV8xNzgwODUyMjk3OjE3ODA5Mzg2OTdfVjM)

发现攻击者使用了名为 "Acunetix" 的 Web 漏洞扫描器。同时确认了该目标网站使用的是 Joomla 内容管理系统 \(CMS\)。

### 漏洞利用与暴力破解 

**溯源思路：** 确定目标是 Web 服务器后，攻击者在扫描无果的情况下选择了对 Joomla 后台进行暴力破解。因为登录需要通过 POST 方法提交表单，可以通过过滤 HTTP POST 请求、目标 IP 以及表单数据字段（包含 username 和 passwd）来定位爆破行为。

```Plain Text
index="botsv1" sourcetype="stream:http" http_method="POST" dest_ip="192.168.250.70" form_data=*username*passwd*
| stats count by src_ip
```

![Image](https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/authcode/?code=OTQzZWIxMDUxZTliMjcwZGFlOGMwMTRhNWM2MzE4ODNfMDI1OTk1MmM4YTkyMTgwZjlmYTFhZjEzMmEyYjE5ZmVfSUQ6NzY0NjY5Njg3NjcwNjY0NzIyOV8xNzgwODUyMjk3OjE3ODA5Mzg2OTdfVjM)

发现源 IP `23.22.63.114` 正在对服务器发起大量爆破请求。

**扩展分析（密码细节）：** 通过附加 `| table _time form_data | reverse` 语句，发现攻击者尝试的第一个密码是 `123456788`。

**扩展分析（爆破结果）：** 通过使用正则表达式 `| rex field=form_data "passwd=(?<userpassword>\w+)" | stats count by userpassword` 提取密码，发现 `batman` 这个密码在日志中出现了两次，这暗示它是最终被成功破解的正确密码。整个过程中，攻击者共尝试了 413 个不重复的密码。

### 恶意载荷安装

**溯源思路：**攻击者在成功爆破获取 Joomla 后台权限后，通常会利用后台的文件上传功能来植入后门或恶意程序。Web 文件上传通常使用 HTTP POST 方法，并且其 `Content-Type` 为 `multipart/form-data`。我们可以通过过滤目标服务器 IP 的 HTTP 流量、限制 POST 请求与表单类型，并直接搜索常见的可执行文件后缀（如 `*.exe`）来精准定位恶意文件的上传行为。

```Plain Text
index =botsv1 sourcetype=stream:http dest_ip= "192.168.250.70" http_method=POST multipart/form-data *.exe
```

通过执行上述包含 `multipart/form-data` 和 `*.exe` 的流量查询，排除了正常的网页访问干扰，精准定位并确认攻击者成功上传了一个名为 `3791.exe` 的可执行文件。

![Image](https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/authcode/?code=ZjBmMTQ5MGViODI1YzJmNzc0NmUyOGNjNGNiYjc1NmFfMjkyMDM4NmZkYzdkYTU3NTE4YmVhZDVmMmRjMTIzMjFfSUQ6NzY0NjY5OTEyNzMyOTk5OTgxMF8xNzgwODUyMjk3OjE3ODA5Mzg2OTdfVjM)

随后，利用获取到的文件名 `3791.exe` 进一步结合 Sysmon 日志进行检索，成功提取到了该恶意文件的 MD5 哈希值。这个哈希值为后续在威胁情报平台（如 VirusTotal）上进行拓线分析提供了最关键的指纹凭证。

```Plain Text
index=botsv1 3791.exe md5 sourcetype="XmlWinEventLog:Microsoft-Windows-Sysmon/Operational" CommandLine="3791.exe" 
| rex field="_raw" "MD5=(?<hash>\w+)" 
| table hash 
| stats count by hash
```

### 达成目标与网页篡改

**溯源思路：** 网站被篡改往往涉及页面内容的替换或外部媒体文件的加载。通过分析 Suricata 流量日志，检查目标 Web 服务器返回的文件内容类型（如 `image/jpeg`），可以找到被替换的网页素材。

```Plain Text
index=botsv1 sourcetype="suricata" src_ip="192.168.250.70" dest_ip="23.22.63.114" 
|  stats count by http.http_method, http.hostname, http.url 
|  sort -count
```

![Image](https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/authcode/?code=OTkwYjMwMDY4ZWRjNzg5ZTc1N2NiNjc5OGYxOTdkYTFfMmUzNTAwMmMwYTAzNWQxMzRhMTBkOGIxYTkzYzY3M2VfSUQ6NzY0NjcwMDA5NzU5NDE2NjIwMF8xNzgwODUyMjk3OjE3ODA5Mzg2OTdfVjM)

过查看 HTTP 的 url 字段，发现了一张恶意的 JPEG 图片。该图片名为 `poisonivy-is-coming-for-you-batman.jpeg`。

进一步分析发现，这张图片托管在攻击者的动态 DNS 域名 `prankglassinebracket.jumpingcrab.com` 上，并且使用了非标准端口 `1337`。该域名直接解析到了之前发现的爆破 IP `23.22.63.114`。

![Image](https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/authcode/?code=OTQ5YmViNjFmNjY5NGFjM2VhZTAyMTNiY2YwN2YzYmNfOTBmYTU1ODAyZjFkZWQwOTE1ZWY5Y2MxZTgzZGVkYmZfSUQ6NzY0NjcwMDk1MzUzMDY5ODk3MF8xNzgwODUyMjk3OjE3ODA5Mzg2OTdfVjM)



## 场景二

感觉有点难以还原，先做场景一

[medium\.com](https://medium.com/@m00hamedw0rk/ransomware-investigation-using-splunk-botsv1-scenario-2-walkthrough-23465aa33c71)

**事件背景：** 2016 年 8 月 24 日，员工 Bob Smith 的 Windows 10 工作站（主机名：`we8105desk`）遭到 Cerber 勒索软件感染，导致本地文件及网络共享文件被全面加密。

### 初始访问

**核心事实：**攻击并非通过钓鱼邮件发起，而是 Bob Smith 捡到了一个遗落在停车场的恶意 USB 闪存盘，将其插入办公电脑后，打开了其中的恶意 Office 宏文档 `Miranda_Tate_unveiled.dotm`

**溯源线索与分析：**为了确认受害主机的 IP 以及插入的 USB 设备记录，我们需要结合 Sysmon 的网络连接日志和 Windows 注册表日志。

```Plain Text
index=botsv1 source="WinEventLog:Microsoft-Windows-Sysmon/Operational" host=we8105desk EventID=3 
| top limit=5 SourceIp
```

![Image](https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/authcode/?code=OTE5MGY1ZjdkMjBkYmEyOWNiZmE1M2NkZDZmNTdlMjNfMDAyOTlkNjg2NjYyOWZiZTM0NmM0MDYxNmUzY2I2ZDJfSUQ6NzY0NjcwMzE4MzQyNjg5OTEzM18xNzgwODUyMjk3OjE3ODA5Mzg2OTdfVjM)

确认受害主机 `we8105desk` 在事发时的核心 IP 地址为 `192.168.250.100`。

```Plain Text
index=botsv1 sourcetype=winregistry friendlyname
```

![Image](https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/authcode/?code=MTM4N2MzYjhiNGZiYjBkZjlmNzY1ODU3YjEyMDhiNjZfNmU0YTM2NjdhNTRmN2Y3ZGNmYjEyYzdhMjZhYzMwOTRfSUQ6NzY0NjcwNjExODA2NDExNDY0Nl8xNzgwODUyMjk3OjE3ODA5Mzg2OTdfVjM)

Windows 注册表会记录 USB 的友好名称。查询发现 Bob 插入的 USB 驱动器名称为 `MIRANDA_PRI`。

### 执行

**核心事实：**Bob 打开文档后，恶意宏在后台静默执行了一段 VBScript。该脚本随后释放并调用了一个名为 `121214.tmp` 的临时执行文件，正式启动勒索软件的核心进程。

**溯源线索与分析：**我们需要分析 Sysmon 的进程创建日志（EventCode=1），抓取命令行（CommandLine）中超长的混淆代码以及恶意子进程。

```Plain Text
index=botsv1 source=WinEventLog:Microsoft-Windows-Sysmon/Operational host=we8105desk vbs
| eval vbslen=len(CommandLine)
| table CommandLine vbslen
```

![Image](https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/authcode/?code=ZTBkNGMyNTU4MjllNDNjM2YzYjFmMDFjYjEyZjUyZmVfNjJlMjBkNTVkOTZlYjBjNzkxZjllYTc3ZWE4MzZkYWFfSUQ6NzY0NjcwNjc1OTUyOTU2NTE1NF8xNzgwODUyMjk3OjE3ODA5Mzg2OTdfVjM)

发现一段由 `cmd.exe` 执行的极长的混淆 VBS 脚本，其命令行长度达到了 4490 个字符。

```Plain Text
index=botsv1 host=we8105desk EventID=1 CommandLine=*121214.tmp* | table _time ProcessId ParentProcessId
```

![Image](https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/authcode/?code=Y2M3NDljZGY2NGY5YzZkYTA4ZDYzODQ1N2U4ZmJlY2RfYmZhMjM5OTIyOTlkNWZiNDk2NDJiYmQ5NWQyNTQxYjZfSUQ6NzY0NjcwNzIwNDkzMDI0MzUzNl8xNzgwODUyMjk3OjE3ODA5Mzg2OTdfVjM)

日志显示，上述恶意脚本作为父进程（ID: 3968），成功拉起了初始的勒索启动文件 `121214.tmp`。

### C2

**核心事实：** 勒索软件在本地启动后，开始向外部恶意的 DNS 服务器发起解析请求以连接 C2 基础设施，并下载了包含 Cerber 真正加密器代码的伪装文件。网络 IDS \(Suricata\) 和防火墙 \(Fortigate\) 记录了这些异常。

```Plain Text
index=botsv1 sourcetype=suricata cerber
| stats count by alert.signature_id
```

![Image](https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/authcode/?code=NzZmYmViN2FjODc5NzFlNmIyYjI4YjI5NTFhZmEyOThfZTIwMjJkZmNiYWQ3ZmE5ZmJhYmQ1MDhiZWI1Yzg0OWVfSUQ6NzY0NjcwODIzMTU0ODk2Mzc5OV8xNzgwODUyMjk3OjE3ODA5Mzg2OTdfVjM)

![Image](https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/authcode/?code=NzFlNTFlMDRkNzU2OTQ4NzIzMzFlOGUyMDczN2Y0MWJfOTk5Y2E1NjUzOGY2YjNjNzVmYjhkMGFlODhkMTY2YzBfSUQ6NzY0NjcwODM3NTEzNTg2NTc4OF8xNzgwODUyMjk3OjE3ODA5Mzg2OTdfVjM)

```Plain Text
index=botsv1 sourcetype=stream:dns src_ip=192.168.250.100 "query_type{}"=A NOT (query{}=*.microsoft.com OR query{}=*.google.com OR query{}=*.waynecorpinc.com)
| table _time query{} src dest
```

![Image](https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/authcode/?code=MTk3MjA2MDJhNzgxOWMxODllNjFlY2VkNjhkM2ExMzFfNzM5YmVlZDMxYjg3NjQ0YzVmNDcxZGNhZjk1MzYzNjFfSUQ6NzY0NjcwODcwMjY1NzcxMTMxM18xNzgwODUyMjk3OjE3ODA5Mzg2OTdfVjM)

除了微软、谷歌和公司内网的白名单域名后，发现受害机访问了首个可疑域名 `solidaritedeproximite.org`。





