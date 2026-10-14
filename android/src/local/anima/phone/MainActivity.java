package local.anima.phone;

import android.app.Activity;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.webkit.WebView;
import android.widget.FrameLayout;

/** Attaches the retained voice session to a screen without owning its lifetime. */
public final class MainActivity extends Activity {
    private PhoneRuntime runtime;
    @Override public void onCreate(Bundle state){
        super.onCreate(state);runtime=((PhoneApplication)getApplication()).session();runtime.attach(this);
        WebView web=runtime.view();if(web.getParent()!=null)((ViewGroup)web.getParent()).removeView(web);
        FrameLayout root=new FrameLayout(this);root.addView(web,new FrameLayout.LayoutParams(-1,-1));setContentView(root);
        if(Build.VERSION.SDK_INT>=30){
            getWindow().setDecorFitsSystemWindows(false);
            root.setOnApplyWindowInsetsListener((view,insets)->{
                android.graphics.Insets bars=insets.getInsets(WindowInsets.Type.systemBars()),ime=insets.getInsets(WindowInsets.Type.ime());
                FrameLayout.LayoutParams layout=(FrameLayout.LayoutParams)web.getLayoutParams();
                layout.leftMargin=bars.left;layout.topMargin=bars.top;layout.rightMargin=bars.right;layout.bottomMargin=Math.max(bars.bottom,ime.bottom);
                web.setLayoutParams(layout);return WindowInsets.CONSUMED;
            });
        }
    }
    @Override protected void onPause(){runtime.onPause();super.onPause();}
    @Override protected void onResume(){super.onResume();runtime.onResume();}
    @Override public void onRequestPermissionsResult(int request,String[] permissions,int[] grants){super.onRequestPermissionsResult(request,permissions,grants);runtime.onRequestPermissionsResult(request,permissions,grants);}
    @Override protected void onActivityResult(int request,int result,Intent data){super.onActivityResult(request,result,data);runtime.onActivityResult(request,result,data);}
    @Override public void onBackPressed(){runtime.onBackPressed();}
    @Override protected void onDestroy(){
        WebView web=runtime.view();if(web!=null && web.getParent()!=null)((ViewGroup)web.getParent()).removeView(web);
        runtime.detach(this);super.onDestroy();
    }
}
