package local.anima.phone;

import android.app.Application;

/** One retained local session; an activity is only its visible front end. */
public final class PhoneApplication extends Application {
    private PhoneRuntime runtime;
    public PhoneRuntime session(){
        if(runtime==null || !runtime.alive())runtime=new PhoneRuntime(this);
        return runtime;
    }
    public PhoneRuntime existingSession(){return runtime!=null && runtime.alive()?runtime:null;}
}
