"""Scripts injetados na página no lugar do compartilhamento de tela.

O navegador não deixa automatizar a escolha da tela, então getDisplayMedia é trocado
por um que devolve vídeo de um canvas e um tom de áudio (ou recusa, como quem cancela).
"""

TELA_COM_AUDIO = """
navigator.mediaDevices.getDisplayMedia = async () => {
  const ctx = new AudioContext();
  const osc = ctx.createOscillator();
  const dest = ctx.createMediaStreamDestination();
  osc.frequency.value = 660; osc.connect(dest); osc.start();
  const canvas = Object.assign(document.createElement("canvas"), { width: 64, height: 64 });
  canvas.getContext("2d").fillRect(0, 0, 64, 64);
  const [video] = canvas.captureStream(5).getVideoTracks();
  return new MediaStream([video, ...dest.stream.getAudioTracks()]);
};
"""

TELA_RECUSADA = """
navigator.mediaDevices.getDisplayMedia = async () => {
  throw new DOMException("Permission denied", "NotAllowedError");
};
"""
