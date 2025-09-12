window.addEventListener('load', () => {
  const loader = document.getElementById('loader');
  const content = document.getElementById('content');
  if(loader && content){
    loader.style.display = 'none';
    content.style.display = 'block';
  }
});
