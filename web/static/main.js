(function () {
  var host = window.location.hostname || "124.221.115.174";
  var port = window.location.port || "8002";

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  setText("ota-default", "http://" + host + ":" + port + "/xiaozhi/ota/");
  setText("ota-english", "http://" + host + ":" + port + "/xiaozhi/ota/english/");
  setText("footer-host", "服务器：" + host + ":" + port);
})();
