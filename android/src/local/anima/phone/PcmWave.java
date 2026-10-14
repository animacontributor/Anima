package local.anima.phone;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/** Decode the server's PCM WAV without changing stereo ear channels. */
public final class PcmWave {
    public final int rate, channels;
    public final float[] samples;
    private PcmWave(int r,int c,float[] s){rate=r;channels=c;samples=s;}
    public static PcmWave decode(byte[] wav) throws Exception {
        ByteBuffer b=ByteBuffer.wrap(wav).order(ByteOrder.LITTLE_ENDIAN);
        if(wav.length<12||b.getInt()!=0x46464952){throw new Exception("Invalid WAV header.");}
        b.getInt();if(b.getInt()!=0x45564157)throw new Exception("Invalid WAV type.");
        int channels=0,rate=0,bits=0,alignment=0,start=-1,size=0;
        while(b.remaining()>=8){
            int tag=b.getInt(),length=b.getInt(),at=b.position();
            if(length<0||length>b.remaining())throw new Exception("Truncated WAV chunk.");
            if(tag==0x20746d66){
                if(length<16||b.getShort()!=1)throw new Exception("Expected PCM WAV.");
                channels=b.getShort();rate=b.getInt();b.getInt();alignment=b.getShort();bits=b.getShort();
            }else if(tag==0x61746164){start=at;size=length;}
            b.position(at+length);
            if((length&1)!=0&&b.hasRemaining())b.get();
        }
        if((channels!=1&&channels!=2)||rate<8000||rate>48000||(bits!=16&&bits!=24)||alignment!=channels*(bits/8)||start<0||size%alignment!=0||size>rate*alignment*35)
            throw new Exception("Unsupported voice PCM format.");
        float[] pcm=new float[size/(bits/8)];b.position(start);
        for(int i=0;i<pcm.length;i++){
            if(bits==16)pcm[i]=b.getShort()/32768f;
            else{int value=(b.get()&255)|((b.get()&255)<<8)|(b.get()<<16);pcm[i]=value/8388608f;}
        }
        return new PcmWave(rate,channels,pcm);
    }
}
