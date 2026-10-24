package cn.dermagent.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.view.ViewGroup;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;

import java.util.ArrayList;

/**
 * 肤智（DermAgent）安卓端：WebView 壳，加载电脑端本机 server（同套对话式界面与数据）。
 * 地址持久化；首次连接失败可改地址；支持文件上传与摄像头授权；明文 http 局域网直连。
 */
public class MainActivity extends Activity {
    private static final String PREFS = "derm";
    private static final String KEY_URL = "base_url";
    private static final String DEFAULT_URL = "http://172.20.10.2:8765/";
    private static final int REQ_FILE = 1001;
    private static final int REQ_PERM = 2001;

    private WebView web;
    private ValueCallback<Uri[]> fileCallback;
    private boolean prompted;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        web = new WebView(this);
        setContentView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setAllowFileAccess(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, String url) {
                Uri u = Uri.parse(url);
                Uri base = Uri.parse(baseUrl());
                if (u.getHost() != null && u.getHost().equals(base.getHost())) return false;
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (Exception ignored) { }
                return true; // 外链交给系统浏览器
            }

            @Override
            public void onReceivedError(WebView v, int code, String desc, String failingUrl) {
                boolean connIssue = code == WebViewClient.ERROR_HOST_LOOKUP
                        || code == WebViewClient.ERROR_CONNECT
                        || code == WebViewClient.ERROR_TIMEOUT;
                if (connIssue && failingUrl != null && failingUrl.startsWith("http") && !prompted) {
                    prompted = true;
                    promptAddress("无法连接电脑端服务，请确认：① 电脑端程序已运行；② 手机与电脑在同一 Wi-Fi/热点网络。");
                }
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb,
                                             FileChooserParams p) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = cb;
                Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("*/*");
                i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                try {
                    startActivityForResult(Intent.createChooser(i, "选择文件"), REQ_FILE);
                } catch (Exception e) {
                    fileCallback = null;
                    return false;
                }
                return true;
            }

            @Override
            public void onPermissionRequest(final PermissionRequest req) {
                runOnUiThread(() -> {
                    ArrayList<String> want = new ArrayList<>();
                    for (String r : req.getResources()) {
                        if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(r))
                            want.add(android.Manifest.permission.CAMERA);
                        if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r))
                            want.add(android.Manifest.permission.RECORD_AUDIO);
                    }
                    if (want.isEmpty()) { req.grant(req.getResources()); return; }
                    requestPermissions(want.toArray(new String[0]), REQ_PERM);
                });
            }
        });
        web.loadUrl(baseUrl());
    }

    private String baseUrl() {
        return getSharedPreferences(PREFS, MODE_PRIVATE).getString(KEY_URL, DEFAULT_URL);
    }

    private void promptAddress(String msg) {
        AlertDialog.Builder b = new AlertDialog.Builder(this);
        b.setTitle("肤智（DermAgent）");
        b.setMessage(msg);
        final EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setText(baseUrl());
        b.setView(input);
        b.setPositiveButton("连接", (d, w) -> {
            String u = input.getText().toString().trim();
            if (!u.isEmpty()) {
                if (!u.startsWith("http")) u = "http://" + u;
                getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(KEY_URL, u).apply();
            }
            prompted = false;
            web.loadUrl(baseUrl());
        });
        b.setNegativeButton("重试", (d, w) -> {
            prompted = false;
            web.loadUrl(baseUrl());
        });
        b.show();
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        if (req == REQ_FILE && fileCallback != null) {
            Uri[] out = null;
            if (res == RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    int n = data.getClipData().getItemCount();
                    out = new Uri[n];
                    for (int i = 0; i < n; i++) out[i] = data.getClipData().getItemAt(i).getUri();
                } else if (data.getData() != null) {
                    out = new Uri[]{data.getData()};
                }
            }
            fileCallback.onReceiveValue(out);
            fileCallback = null;
        } else {
            super.onActivityResult(req, res, data);
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        if (code == REQ_PERM && web != null) web.reload(); // 授权后重载，让页面重新发起请求
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }
}
