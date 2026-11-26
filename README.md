<font style="color:rgb(0,0,0);">启动服务器:</font>

<font style="color:rgb(0,0,0);">项目根目录下直接启动server.py即可</font>

```plain
python server.py
```

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795528587165-c588ba4b-335f-43a2-93a4-3c8105a283ee.png)

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795528608773-0f22a302-db50-4bf6-aa33-02b8dd35c1ec.png)

## <font style="color:rgb(0,0,0);">请求解析</font>

<font style="color:rgb(0,0,0);">实现 HTTP/1.1 请求行、首部与报文的解析</font>

<font style="color:rgb(0,0,0);">使用抓包软件yakit(也可以用curl命令行)：</font>

<font style="color:rgb(0,0,0);">点击：</font>

```plain
http://127.0.0.1:8080/api/echo
```

是请求回显，抓包：

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795528767920-035fb12c-ccb5-4b2b-98c6-f4b21d157b96.png)

行，首部和报文均解析并回显。

## <font style="color:rgb(0,0,0);">方法支持</font>

<font style="color:rgb(0,0,0);">实现 GET 静态文件、目录列表与 POST 表单处理</font>

<font style="color:rgb(0,0,0);">点击目录列表随机点击其中一个文件并抓包，比如sam.txt:</font>

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795528895376-4b8afcf0-131d-4858-8f03-b0a05de74c87.png)

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795528990090-7771f56f-3554-46b9-8075-70356d43006c.png)

<font style="color:rgb(0,0,0);">对目录列表抓包：</font>

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795529360185-cfeb58aa-e3e9-43de-b3cb-d3315ab374a0.png)

是get请求。回显是301，然后会跳转至/files/

这里说明一个区别：

> /files 和 /files/ 是两个不同的东西，/files/ 你知道他是一个文件夹，但是/files 你不确定它是文件还是文件夹。

<font style="color:rgb(0,0,0);">POST 表单处理：</font>

<font style="color:rgb(0,0,0);">点击POST表单，填写表单信息后提交并抓包：</font>

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795529680080-68f9573e-8d87-49e3-8d7a-2cdd76a1c65f.png)

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795529729397-0d60bf1b-c850-4e0a-b728-d806e6781168.png)

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795529753244-6d1f7689-c7f5-4eaa-9680-1fe09e44aa0c.png)

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795529766360-35ede79e-f9d6-40c7-9748-67980d74bb80.png)

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795529774696-63dfab48-7a9a-44dd-8fd1-2428e215eee0.png)

可以看到向后端传了name,email,topic和message等参数。

## <font style="color:rgb(0,0,0);">并发处理</font>

<font style="color:rgb(0,0,0);">采用多线程或 epoll 支持多连接并发这个会在压测里一同体现。</font>

## <font style="color:rgb(0,0,0);">连接特性</font>

<font style="color:rgb(0,0,0);">支持 Keep-Alive 持久连接与虚拟主机</font>

<font style="color:rgb(0,0,0);">有健康检查：</font>

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795529932003-4649c3bb-a873-407f-9fd2-b85e7b9d0e57.png)

虚拟主机：

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795529954154-ef557cea-17c8-484e-85aa-f691c1996905.png)

## <font style="color:rgb(0,0,0);">状态码</font>

<font style="color:rgb(0,0,0);">正确返回 200、301、403、404、500 等状态码并记录访问日志</font>

<font style="color:rgb(0,0,0);">200:</font>

<font style="color:rgb(0,0,0);">post表单案例：</font>

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795530012503-ebbe9069-1d57-4795-a468-7db4b9b94c56.png)

<font style="color:rgb(0,0,0);">301：</font>

<font style="color:rgb(0,0,0);">/files 案例：</font>

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795530044617-b0f9ed30-6992-4d2f-ab1d-a7eeed1b5eee.png)

<font style="color:rgb(0,0,0);">403：</font>

<font style="color:rgb(0,0,0);">secret.txt案例：</font>

```plain
http://localhost:8080/private/secret.txt
```

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795530112970-8efa6b95-aa30-4c2b-8bf0-9dd65c0c8839.png)

<font style="color:rgb(0,0,0);">404：</font>

<font style="color:rgb(0,0,0);">missing案例或者任意一个不存在的路径</font>

```plain
http://localhost:8080/missing
http://localhost:8080/123
```

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795530188990-2e9b153e-98a7-4602-9021-0847fe1d4c62.png)

500：

这里就另起一个服务了,为什么呢：

这个 500 异常会被服务器捕获，返回 500 页面并关闭当前连接，不会让服务器进程退出。为了不让服务器关闭连接我们需要另起一个端口和8080端口运行一样的服务以测试。

```plain
python server.py --port 8082 --test-mode
```

访问：

```plain
http://127.0.0.1:8082/__test__/error
```

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795530493994-23a1c470-5361-43db-8aa4-b86256fd64fa.png)

<font style="color:rgb(0,0,0);">记录访问日志：</font>

<font style="color:rgb(0,0,0);">1.命令行会回显：</font>

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795530553043-2caef702-5472-4fa3-a934-2d43765cc867.png)

2.根目录/logs文件夹会保存日志：

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795530612820-eb3af216-6fe6-46e0-9d23-cfd79f12de17.png)

## <font style="color:rgb(0,0,0);">压测验证</font>

<font style="color:rgb(0,0,0);">用 ab、curl 对本机做并发测试并分析性能</font>

<font style="color:rgb(0,0,0);">用个压测脚本bench.py即可：</font>

```plain
python bench.py
```

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795531481494-4e2532fc-eaa1-473b-a6e3-735037a35eb3.png)

![](https://cdn.nlark.com/yuque/0/2026/png/65088136/1795531489814-6f671046-594d-483d-993c-2f21ce755ba6.png)

