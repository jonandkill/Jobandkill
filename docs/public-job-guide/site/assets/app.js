(function () {
  "use strict";

  var data = Array.isArray(window.PUBLIC_JOB_DATA) ? window.PUBLIC_JOB_DATA : [];
  var pageSize = 50;
  var visible = pageSize;
  var form = document.getElementById("filters");
  var tbody = document.getElementById("results");
  var count = document.getElementById("result-count");
  var more = document.getElementById("more");

  function unique(field) {
    return Array.from(new Set(data.map(function (row) { return row[field]; }).filter(Boolean))).sort(function (a, b) {
      return a.localeCompare(b, "ko");
    });
  }

  function addOptions(id, field) {
    var select = document.getElementById(id);
    unique(field).forEach(function (value) {
      var option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      select.appendChild(option);
    });
  }

  addOptions("source", "source");
  addOptions("track", "track");
  addOptions("fit", "fit");

  function text(id) {
    return document.getElementById(id).value.trim().toLocaleLowerCase("ko");
  }

  function matches(row) {
    var query = text("query");
    var source = text("source");
    var route = text("track");
    var fit = text("fit");
    var region = text("region");
    var quality = text("quality");
    var haystack = [row.institution, row.title, row.employment, row.track, row.qualification, row.certificates].join(" ").toLocaleLowerCase("ko");
    if (query && !haystack.includes(query)) return false;
    if (source && row.source.toLocaleLowerCase("ko") !== source) return false;
    if (route && row.track.toLocaleLowerCase("ko") !== route) return false;
    if (fit && row.fit.toLocaleLowerCase("ko") !== fit) return false;
    if (region && !row.region.toLocaleLowerCase("ko").includes(region)) return false;
    if (quality === "정상" && row.quality !== "정상") return false;
    if (quality === "확인" && row.quality === "정상") return false;
    return true;
  }

  function cell(row, value, className) {
    var td = document.createElement("td");
    td.textContent = value || "";
    if (className) td.className = className;
    row.appendChild(td);
    return td;
  }

  function render() {
    var filtered = data.filter(matches);
    tbody.replaceChildren();
    filtered.slice(0, visible).forEach(function (item) {
      var tr = document.createElement("tr");
      cell(tr, item.institution);
      cell(tr, item.title);
      var sourceCell = cell(tr, "");
      var chip = document.createElement("span");
      chip.className = "source-chip";
      chip.textContent = item.source;
      sourceCell.appendChild(chip);
      cell(tr, item.region);
      cell(tr, item.employment);
      cell(tr, item.track);
      cell(tr, item.fit);
      cell(tr, item.deadline, "date");
      cell(tr, item.regionRestriction);
      var qualityCell = cell(tr, item.quality);
      if (item.quality !== "정상") qualityCell.className = "quality-warning";
      var linkCell = cell(tr, "");
      var link = document.createElement("a");
      link.href = item.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "확인";
      linkCell.appendChild(link);
      tbody.appendChild(tr);
    });
    count.textContent = "검색 결과 " + filtered.length.toLocaleString("ko-KR") + "건 · " + Math.min(filtered.length, visible).toLocaleString("ko-KR") + "건 표시";
    more.hidden = visible >= filtered.length;
  }

  form.addEventListener("input", function () {
    visible = pageSize;
    render();
  });
  form.addEventListener("change", function () {
    visible = pageSize;
    render();
  });
  more.addEventListener("click", function () {
    visible += pageSize;
    render();
  });

  render();
}());

