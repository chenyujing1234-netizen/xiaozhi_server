(function () {
  var host = window.location.hostname || "124.221.115.174";
  var protocol = window.location.protocol || "http:";
  var isIp = /^\d+\.\d+\.\d+\.\d+$/.test(host);
  var isLinkpal =
    host === "linkpal.cloud" || host === "www.linkpal.cloud";
  var webDomain = isLinkpal ? "linkpal.cloud" : (isIp ? "linkpal.cloud" : host);

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  if (isLinkpal && protocol === "https:") {
    var base = "https://" + host;
    setText("ota-default", base + "/xiaozhi/ota/");
    setText("ota-english", base + "/xiaozhi/ota/english/");
    setText("web-english", "https://linkpal.cloud/english/");
    setText("footer-host", "官网：" + base + "/");
  } else {
    var port = window.location.port || "8002";
    setText("ota-default", "http://" + host + ":" + port + "/xiaozhi/ota/");
    setText("ota-english", "http://" + host + ":" + port + "/xiaozhi/ota/english/");
    setText("web-english", "https://" + webDomain + "/english/");
    setText("footer-host", "服务器：" + host + ":" + port);
  }
})();
